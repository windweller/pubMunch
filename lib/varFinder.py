# find descriptions of variants in text
import logging, gdbm, marshal, zlib, copy, struct, random, sqlite3, types
from collections import defaultdict, namedtuple
from os.path import join

logger = None

# re2 is often faster than re
# we fallback to re just in case
try:
    import re2 as re
except ImportError:
    import re

from pubSeqTables import threeToOneLower, threeToOne, oneToThree, aaToDna, dnaToAa
from pm_pycbio.hgdata.Psl import Psl
import pslMapBed, pubAlg, maxbio, pubConf, maxCommon, pubKeyVal

from pygr.seqdb import SequenceFileDB

regexes = None

# this setting can be changed to allow protein variants
# that require a change of two base pairs. By default, it
# is off to reduce false positives
allowTwoBpVariants = False

# ===== DATA TYPES ========
Mention = namedtuple("Mention", "patName,start,end")

""" A mapped variant is a type-range-sequence combination from a text,
    can be located on none, one or multiple types of sequences
    All positions are 0-based
"""
VariantFields = [
    "mutType",  # sub, del or ins
    "seqType",  # prot, dna or dbSnp
    "seqId",  # protein or nucleotide accession
    "geneId",  # entrez gene id, if a gene was found nearby in text
    "start",  # original position in text
    "end",  # end position in text
    "origSeq",  # wild type seq, used for sub and del, also used to store rsId for dbSnp variants
    "mutSeq",  # mutated seq, used for sub and ins
    "origStr",  # variant name as it occurs in the original paper
    "offset", # offset for splicing variants, e.g., -3 for c.1184-3A>T
    ]

# A completely resolved mutation
mutFields = \
    (
    "chrom",  # chromosome
    "start",  # on chrom
    "end",  # on chrom
    "offset", # intron offset
    "varId",  # a unique id
    "inDb",  # list of db names where was found
    "patType",  # the type of the patterns (sub, del, ins)
    "hgvsProt",  # hgvs on protein, can be multiple, separated with |
    "hgvsCoding",  # hgvs on cdna, can be multiple, separated with |
    "hgvsRna",  # hgvs on refseq, separated by "|"
    "comment",  # comment on how mapping was done
    "rsIds",  # possible rsIds, separated by "|", obtained by mapping from hgvsRna
    "protId",  # the protein ID that was used for the first mapping
    "texts",  # mutation match in text
    # "mutSupport",  # prot, dna, protDna
    # "mutCount",    # how often was this mutation mentioned?
    "rsIdsMentioned",  # mentioned dbSnp IDs that support any of the hgvsRna mutations
    "dbSnpStarts" ,  # mentioned dbSnp IDs in text, start positions
    "dbSnpEnds",  # mentioned dbSNP Ids in text, end positions

    "geneSymbol",  # symbol of gene
    "geneType",  # why was this gene selected (entrez, symNearby, symInTitle, symInAbstract)
    "entrezId",  # entrez ID of gene
    "geneStarts",  # start positions of gene mentions in document
    "geneEnds",  # end positions of gene mentions in document

    "seqType",  # the seqType of the patterns, dna or protein
    "mutPatNames",  # the names of the patterns that matched, separated by |
    "mutStarts",  # start positions of mutation pattern matches in document
    "mutEnds",  # end positions of mutation pattern matches in document
    "mutSnippets",  # the phrases around the mutation mentions, separated by "|"
    "geneSnippets",  # the phrases around the gene mentions, separated by "|"
    "dbSnpSnippets"  # mentioned dbSNP Ids in text, snippets
    )

# fields of the output file
MutRec = namedtuple("mutation_desc", mutFields)


# ======= GLOBALS ===============
# this can be used to shuffle all protein sequences before matching
# to get a random background estimate
doShuffle = False

geneData = None

# these look like mutations but are definitely not mutations
blackList = set([
    ("E", 2, "F"),  # genes
    ("D", 11, "S"),  # satellites
    ("D", 12, "S"),
    ("D", 13, "S"),
    ("D", 14, "S"),
    ("D", 15, "S"),
    ("D", 16, "S"),
    ("A", 84, "M"),  # cell lines...
    ("A", 84, "P"),  # all of these copied...
    ("A", 94, "P"),  # from http://bioinf.umbc.edu/EMU/ftp/Cell_line_list_short.txt
    ("A", 94, "P"),
    ("C", 127, "I"),
    ("C", 86, "M"),
    ("C", 86, "P"),
    ("L", 283, "R"),
    ("H", 96, "V"),
    ("L", 5178, "Y"),
    ("L", 89, "M"),
    ("L", 89, "P"),
    ("L", 929, "S"),
    ("T", 89, "G"),
    ("T", 47, "D"),
    ("T", 84, "M"),
    ("T", 98, "G"),
    ("S", 288, "C"),  # yeast strain names
    ("T", 229, "C"),

    # these are from cellosaurus:
    # "pubPrepGeneDir cells" to re-generate this list
    ('F', 442, 'A'), ('A', 101, 'D'), ('A', 2, 'H'), ('A', 375, 'M'), ('A', 375, 'P'), ('A', 529, 'L'), ('A', 6, 'L'), ('B', 10, 'R'), ('B', 10, 'S'), ('B', 1203, 'L'), ('C', 2, 'M'), ('C', 2, 'W'), ('B', 16, 'V'), ('B', 35, 'M'), ('B', 3, 'D'), ('B', 46, 'M'), ('C', 33, 'A'), ('C', 4, 'I'), ('C', 127, 'I'), ('C', 463, 'A'), ('C', 611, 'B'), ('C', 831, 'L'), ('D', 18, 'T'), ('D', 1, 'B'), ('D', 2, 'N'), ('D', 422, 'T'), ('D', 8, 'G'), ('F', 36, 'E'), ('F', 36, 'P'), ('F', 11, 'G'), ('F', 1, 'B'), ('F', 4, 'N'), ('G', 14, 'D'), ('G', 1, 'B'), ('G', 1, 'E'), ('H', 2, 'M'), ('H', 2, 'P'), ('H', 48, 'N'), ('H', 4, 'M'), ('H', 4, 'S'), ('H', 69, 'V'), ('C', 3, 'A'), ('C', 1, 'R'), ('H', 766, 'T'), ('I', 51, 'T'), ('K', 562, 'R'), ('L', 5178, 'Y'), ('L', 2, 'C'), ('L', 929, 'S'), ('M', 59, 'K'), ('M', 10, 'K'), ('M', 10, 'T'), ('M', 14, 'K'), ('M', 22, 'K'), ('M', 24, 'K'), ('M', 25, 'K'), ('M', 28, 'K'), ('M', 33, 'K'), ('M', 38, 'K'), ('M', 9, 'A'), ('M', 9, 'K'), ('H', 1755, 'A'), ('H', 295, 'A'), ('H', 295, 'R'), ('H', 322, 'M'), ('H', 460, 'M'), ('H', 510, 'A'), ('H', 676, 'B'), ('P', 3, 'D'), ('R', 201, 'C'), ('R', 2, 'C'), ('S', 16, 'Y'), ('S', 594, 'S'), ('N', 303, 'L'), ('N', 1003, 'L'), ('N', 2307, 'L'), ('N', 1108, 'L'), ('T', 47, 'D'), ('T', 27, 'A'), ('T', 88, 'M'), ('T', 98, 'G'), ('H', 5, 'D'), ('C', 1, 'A'), ('C', 1, 'D'), ('C', 2, 'D'), ('C', 2, 'G'), ('C', 2, 'H'), ('C', 2, 'N'), ('V', 79, 'B'), ('V', 9, 'P'), ('V', 10, 'M'), ('V', 9, 'M'), ('X', 16, 'C')
])

# ===== FUNCTIONS TO INIT THE GLOBALS =================

def loadDb(logLevel=logging.DEBUG, loadSequences=True):
    """
    Initialize the basic databases that this module needs to be able to work.
    Sorry, I have no idea how to work around this.
    >>> loadDb()
    """
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logLevel)
    logger.info("Loading gene information for varFinder.py")
    if loadSequences:
        global geneData
        geneData = SeqData(9606)
    global regexes
    regexes = parseRegex(pubConf.varDataDir)
    logger.info("Blacklist has %d entries" % len(blackList))
    global genomeseq
    genomeseq = SequenceFileDB(join(pubConf.varDataDir, 'hg19.fa'))

def openIndexedPsls(mutDataDir, fileBaseName):
    " return a dict-like object that returns psls given a transcript ID "
    liftFname = join(mutDataDir, fileBaseName)
    logger.debug("Opening %s" % liftFname)
    pslDict = pubKeyVal.SqliteKvDb(liftFname)
    return pslDict

def parseEntrez(fname):
    """ parse a tab-sep table with headers and return one dict with entrez to refprots
    and another dict with entrez to symbol
    """
    entrez2Sym = dict()
    entrez2RefseqProts = dict()
    entrez2RefseqCodingSeqs = dict()

    for row in maxCommon.iterTsvRows(fname):
        entrez2Sym[int(row.entrezId)] = row.sym
        if row.refseqProtIds == "":
            refProts = None
        else:
            refProts = row.refseqProtIds.split(",")
            # assert(len(refProts)==len(refseqs))
        if row.refseqIds == "":
            refseqIds = None
        else:
            refseqIds = row.refseqIds.split(",")

        entrez2RefseqProts[int(row.entrezId)] = refProts
        entrez2RefseqCodingSeqs[int(row.entrezId)] = refseqIds
    return entrez2Sym, entrez2RefseqProts, entrez2RefseqCodingSeqs


# ===== CLASSES =================
class SeqData(object):
    """ functions to get sequences and map between identifiers for entrez genes,
    uniprot, refseq, etc """

    def __init__(self, taxId):
        " open db files, compile patterns, parse input as far as possible "
        mutDataDir = pubConf.varDataDir
        geneDataDir = pubConf.geneDataDir
        if mutDataDir == None:
            return
        self.mutDataDir = mutDataDir
        self.entrez2sym, self.entrez2refprots, self.entrez2refseqs = parseEntrez(join(geneDataDir, "entrez.tab"))
        self.symToEntrez = None  # lazy loading

        # refseq sequences
        fname = join(mutDataDir, "seqs")
        logger.info("opening %s" % fname)
        seqs = pubKeyVal.SqliteKvDb(fname)
        self.seqs = seqs

        # refprot to refseqId
        # refseq to CDS Start
        fname = join(mutDataDir, "refseqInfo.tab")
        logger.debug("Reading %s" % fname)
        self.refProtToRefSeq = {}
        self.refSeqCds = {}
        for row in maxCommon.iterTsvRows(fname):
            self.refProtToRefSeq[row.refProt] = row.refSeq
            self.refSeqCds[row.refSeq] = int(row.cdsStart) - 1  # NCBI is 1-based

        # refseq to genome
        self.pslCache = {}
        self.refGenePsls = openIndexedPsls(mutDataDir, "refGenePsls.9606")

        # dbsnp db
        fname = join(self.mutDataDir, "dbSnp.sqlite")
        self.snpDb = sqlite3.connect(fname)

        logger.info("Reading of data finished")


    def getSeq(self, seqId):
        " get seq from db , cache results "
        logger.log(5, "Looking up sequence for id %s" % seqId)
        seqId = str(seqId)  # no unicode
        if seqId not in self.seqs:
            return None
        return self.seqs[seqId]

    def lookupDbSnp(self, chrom, start, end):
        " return the rs-Id of a position or None if not found "
        # TABLE data (chrom TEXT, start INT, end INT, rsId INT PRIMARY KEY);
        sql = 'SELECT rsId from data where chrom=? and start=? and end=?'
        cur = self.snpDb.execute(sql, (chrom, start, end))
        row = cur.fetchone()
        if row is None:
            return None
        else:
            return "rs" + str(row[0])

    def rsIdToGenome(self, rsId):
        " given the rs-Id, return chrom, start, end of it"
        rsId = int(rsId)
        sql = 'SELECT chrom, start, end from data where rsId=?'
        cur = self.snpDb.execute(sql, (rsId,))
        row = cur.fetchone()
        if row is None:
            return None, None, None
        else:
            return row[0], row[1], row[2]

    def entrezToProtDbIds(self, entrezGene, db):
        " return protein accessions (list) in otherDb for entrezGene "
        entrezGene = int(entrezGene)
        if db == "refseq":
            protIds = self.mapEntrezToRefseqProts(entrezGene)
        # used to have other DBs here
        else:
            assert(False)
        return protIds

    def entrezToCodingSeqDbIds(self, entrezGene, db):
        " return protein accessions (list) in otherDb for entrezGene "
        entrezGene = int(entrezGene)
        if db == "refseq":
            seqIds = self.mapEntrezToRefseqCodingSeq(entrezGene)
        # used to have other DBs here
        else:
            assert(False)
        return seqIds

    def entrezToSym(self, entrezGene):
        entrezGene = str(entrezGene)
        if "/" in entrezGene:
            logger.debug("Got multiple entrez genes %s. Using only first to get symbol." % entrezGene)
        entrezGene = entrezGene.split("/")[0]

        entrezGene = int(entrezGene)
        if entrezGene in self.entrez2sym:
            geneSym = self.entrez2sym[entrezGene]
            logger.debug("Entrez gene %s = symbol %s" % (entrezGene, geneSym))
            return geneSym
        else:
            return None

    def mapSymToEntrez(self, sym):
        " return a list of entrez IDs for given symbol "
        if self.symToEntrez == None:
            self.symToEntrez = defaultdict(list)
            for e, s in self.entrez2sym.iteritems():
                self.symToEntrez[s].append(e)
        entrezIds = self.symToEntrez.get(sym)
        return entrezIds

    def mapEntrezToRefseqProts(self, entrezGene):
        " map entrez gene to refseq prots like NP_xxx "
        if entrezGene not in self.entrez2refprots:
            logger.debug("gene %s is not valid or not in selected species" % str(entrezGene))
            return []

        protIds = self.entrez2refprots[entrezGene]
        if protIds is None:
            logger.debug("gene %s is a non-coding gene, no protein seq available")
            return []

        logger.debug("Entrez gene %d is mapped to proteins %s" % \
            (entrezGene, ",".join(protIds)))
        return protIds

    def mapEntrezToRefseqCodingSeq(self, entrezGene):
        if entrezGene not in self.entrez2refseqs:
            logger.debug("gene %s is not valid or not in selected species" % str(entrezGene))
            return []

        seqIds = self.entrez2refseqs[entrezGene]
        if seqIds is None:
            logger.debug("gene %s is a non-coding gene, no coding seq available")
            return []

        logger.debug("Entrez gene %d is mapped to coding sequence %s" % \
            (entrezGene, ",".join(seqIds)))
        return seqIds

    def getCdsStart(self, refseqId):
        " return refseq CDS start position "
        cdsStart = self.refSeqCds[refseqId]
        return cdsStart

    def getRefSeqId(self, refProtId):
        " resolve refprot -> refseq using refseq data "
        refseqId = self.refProtToRefSeq.get(refProtId, None)
        return refseqId

    def getProteinPsls(self, db, protId):
        if db == "uniprot":
            return self.getUniprotPsls(protId)
        # elif db=="oldRefseq":
            # return self.getOldRefseqPsls(protId)
        # elif db=="genbank":
            # return self.getGenbankPsls(protId)
        else:
            assert(False)

    def getRefseqPsls(self, refseqId):
        """ return psl objects for regseq Id
            as UCSC refseq track doesn't support version numbers, we're stripping those on input
        """
        psls = getPsls(refseqId, self.pslCache, self.refGenePsls, stripVersion=True)
        return psls

    # end of class seqData

class VariantDescription(object):
    """ A variant description fully describes a variant
        It has at least a type (sub, del, etc), a start-end position on a
        (potentially unknown) sequence a tuple (origSeq, mutSeq) that describes
        the mutation e.g. ("R", "S"). The class can generate a descriptive name
        for the mutation like "p.R123S"

        It can optionally include a sequence ID, when the sequence ID was part of the
        mutation description in the text, e.g. the HGVS "NM_000925:p.R123S"

    >>> VariantDescription("sub", "prot", 10, 11, "R", "S")
    VariantDescription(mutType=u'sub',seqType=u'prot',seqId=u'None',geneId=u'',start=u'10',end=u'11',origSeq=u'R',mutSeq=u'S')
    """
    __slots__ = VariantFields

    def __init__(self, mutType, seqType, start, end, origSeq, mutSeq, seqId=None, geneId="", origStr="", offset=0):
        self.mutType = mutType  # sub, del or ins or dbSnp
        self.seqType = seqType  # cds, rna or prot, intron
        self.seqId = seqId
        self.geneId = geneId
        self.start = int(start)
        self.end = int(end)
        self.origSeq = origSeq
        self.mutSeq = mutSeq
        self.origStr = origStr
        self.offset = offset

    def getName(self):
        " return HGVS text for this variant "
        # logger.debug("Creating HGVS type %s for vars %s" % (hgvsType, variants))
        if self.seqId == None:
            name = "p.%s%d%s" % (self.origSeq, self.start, self.mutSeq)
        elif self.mutType == "dbSnp":
            name = self.origSeq
        else:
            name = makeHgvsStr(self.seqType, self.seqId, self.origSeq, self.start, self.mutSeq, self.offset)
        return name

    def asRow(self):
        row = []
        for i in self.__slots__:
            row.append(unicode(getattr(self, i)))
        return row

    def __repr__(self):
        # return ",".join(self.asRow())
        parts = []
        for field in self.__slots__:
            parts.append(field + "=" + repr(unicode(getattr(self, field))))
        return "VariantDescription(%s)" % ",".join(parts)

class SeqVariantData(object):
    """ the full information about variant located on a sequence, with mentions from the text that support it
        This is the final output of this module, including all information about mapped variants and their genes.

    """
    __slots__ = mutFields

    def __init__(self, varId="", protVars=[], codVars=[], rnaVars=[], \
            comment="", beds=[], entrezGene="", geneSym="", rsIds=[], \
            dbSnpMentionsByRsId={}, mentions=[], text="", seqType="prot", patType="sub"):
        self.varId = varId
        self.inDb = ""
        self.patType = patType
        self.seqType = seqType
        self.chrom = ""
        self.start = ""
        self.end = ""
        self.offset = ""
        self.geneSymbol = geneSym
        self.entrezId = entrezGene
        self.hgvsProt = "|".join([v.getName() for v in protVars])
        self.hgvsCoding = "|".join([v.getName() for v in codVars])
        self.hgvsRna = "|".join([v.getName() for v in rnaVars])
        self.comment = comment
        self.rsIds = "|".join(rsIds)
        self.protId = ""
        self.geneType = "entrez"
        self.geneStarts = ""
        self.geneEnds = ""
        self.geneSnippets = ""

        # for each rsId mentioned, concat their starts/ends/snips
        # for each mention there is one rsId in rsIdsMentioned
        starts = []
        ends = []
        snippets = []
        mentionedRsIds = []
        for rsId, mentions in dbSnpMentionsByRsId.iteritems():
            rsStarts, rsEnds, rsPatNames, rsSnips, rsTexts = mentionsFields(mentions, text)
            starts.extend(rsStarts)
            ends.extend(rsEnds)
            snippets.extend(rsSnips)
            # for each mention there is one rsId
            for m in mentions:
                mentionedRsIds.append(rsId)
        self.dbSnpStarts = ",".join(starts)
        self.dbSnpEnds = ",".join(ends)
        self.dbSnpSnippets = "|".join(snippets)
        self.rsIdsMentioned = "|".join(mentionedRsIds)

        mutStarts, mutEnds, patNames, snippets, texts = mentionsFields(mentions, text)
        self.mutStarts = ",".join(mutStarts)
        self.mutEnds = ",".join(mutEnds)
        self.mutPatNames = "|".join(patNames)
        self.mutSnippets = "|".join(snippets)
        self.texts = "|".join(set(texts))

    def asRow(self, rawStr=False):
        row = []
        for i in self.__slots__:
            s = getattr(self, i)
            if rawStr:
                s = str(s)
            else:
                s = unicode(s)
            row.append(s)
        return row

    def __repr__(self):
        # return ",".join(self.asRow())
        parts = []
        for field in self.__slots__:
            parts.append(field + "=" + repr(unicode(getattr(self, field))))
        return "SeqVariantData(%s)" % ",".join(parts)

# ===== FUNCTIONS =================
# helper methods for SeqData
def getPsls(qId, cache, dbm, stripVersion=False):
    """ load psls from compressed dbm, create Psl objects, use a cache
    reverse complement is on negative strand
    """
    qId = str(qId)
    if stripVersion:
        qId = str(qId).split(".")[0]
    logger.debug("Getting mapping psl for %s" % qId)
    if qId in cache:
        psls = cache[qId]
    else:
        if not qId in dbm:
            logger.error("Could not find PSL for %s" % qId)
            return []
        pslLines = dbm[qId]
        psls = []
        for line in pslLines.split("\n"):
            psl = Psl(line.split("\t"))
            psls.append(psl)
        cache[qId] = psls
    logger.debug("Got mapping psl %s" % str(psls[0]))

    corrPsls = []
    for p in psls:
        if p.strand == "-":
            p2 = p.reverseComplement()
        else:
            p2 = p
        corrPsls.append(p2)

    return corrPsls

def makeMention(match, patName):
    start = match.start()
    end = match.end()
    mention = Mention(patName, start, end)
    return mention

def parseRegex(mutDataDir):
    """ parse and compile regexes to list (seqType, mutType, patName, pat) """
    # read regexes, translate placeholders to long form and compile
    replDict = {
    "sep"         : r"""(?:^|[:;\s\(\[\'\"/,\-])""",
    "fromPos"     : r'(?P<fromPos>[1-9][0-9]*)',
    "toPos"       : r'(?P<toPos>[1-9][0-9]*)',
    "pos"         : r'(?P<pos>[1-9][0-9]*)',
    "offset"         : r'(?P<offset>[1-9][0-9]*)',
    "plusMinus"         : r'(?P<plusMinus>[+-])',
    "origAaShort" : r'(?P<origAaShort>[CISQMNPKDTFAGHLRWVEYX])',
    "origAasShort" : r'(?P<origAasShort>[CISQMNPKDTFAGHLRWVEYX]+)',
    "skipAa"  : r'(CYS|ILE|SER|GLN|MET|ASN|PRO|LYS|ASP|THR|PHE|ALA|GLY|HIS|LEU|ARG|TRP|VAL|GLU|TYR|TER|GLUTAMINE|GLUTAMIC ACID|LEUCINE|VALINE|ISOLEUCINE|LYSINE|ALANINE|GLYCINE|ASPARTATE|METHIONINE|THREONINE|HISTIDINE|ASPARTIC ACID|ARGININE|ASPARAGINE|TRYPTOPHAN|PROLINE|PHENYLALANINE|CYSTEINE|SERINE|GLUTAMATE|TYROSINE|STOP|X)',
    "origAaLong"  : r'(?P<origAaLong>(CYS|ILE|SER|GLN|MET|ASN|PRO|LYS|ASP|THR|PHE|ALA|GLY|HIS|LEU|ARG|TRP|VAL|GLU|TYR|TER|GLUTAMINE|GLUTAMIC ACID|LEUCINE|VALINE|ISOLEUCINE|LYSINE|ALANINE|GLYCINE|ASPARTATE|METHIONINE|THREONINE|HISTIDINE|ASPARTIC ACID|ARGININE|ASPARAGINE|TRYPTOPHAN|PROLINE|PHENYLALANINE|CYSTEINE|SERINE|GLUTAMATE|TYROSINE|STOP|X))',
    "origAasLong"  : r'(?P<origAasLong>(CYS|ILE|SER|GLN|MET|ASN|PRO|LYS|ASP|THR|PHE|ALA|GLY|HIS|LEU|ARG|TRP|VAL|GLU|TYR|TER|GLUTAMINE|GLUTAMIC ACID|LEUCINE|VALINE|ISOLEUCINE|LYSINE|ALANINE|GLYCINE|ASPARTATE|METHIONINE|THREONINE|HISTIDINE|ASPARTIC ACID|ARGININE|ASPARAGINE|TRYPTOPHAN|PROLINE|PHENYLALANINE|CYSTEINE|SERINE|GLUTAMATE|TYROSINE|STOP|X)+)',
    "mutAaShort"  : r'(?P<mutAaShort>[fCISQMNPKDTFAGHLRWVEYX*])',  # tolerate "fs"
    "mutAaLong"  : r'(?P<mutAaLong>(CYS|ILE|SER|GLN|MET|ASN|PRO|LYS|ASP|THR|PHE|ALA|GLY|HIS|LEU|ARG|TRP|VAL|GLU|TYR|TER|GLUTAMINE|GLUTAMIC ACID|LEUCINE|VALINE|ISOLEUCINE|LYSINE|ALANINE|GLYCINE|ASPARTATE|METHIONINE|THREONINE|HISTIDINE|ASPARTIC ACID|ARGININE|ASPARAGINE|TRYPTOPHAN|PROLINE|PHENYLALANINE|CYSTEINE|SERINE|GLUTAMATE|TYROSINE|STOP|X|FS))',
    "mutAasShort"  : r'(?P<mutAasShort>[fCISQMNPKDTFAGHLRWVEYX*]+)',  # tolerate "fs"
    "mutAasLong"  : r'(?P<mutAasLong>(CYS|ILE|SER|GLN|MET|ASN|PRO|LYS|ASP|THR|PHE|ALA|GLY|HIS|LEU|ARG|TRP|VAL|GLU|TYR|TER|GLUTAMINE|GLUTAMIC ACID|LEUCINE|VALINE|ISOLEUCINE|LYSINE|ALANINE|GLYCINE|ASPARTATE|METHIONINE|THREONINE|HISTIDINE|ASPARTIC ACID|ARGININE|ASPARAGINE|TRYPTOPHAN|PROLINE|PHENYLALANINE|CYSTEINE|SERINE|GLUTAMATE|TYROSINE|STOP|X|FS)+)',
    "dna"         : r'(?P<dna>[actgACTG])',
    "dnas"         : r'(?P<dnas>[actgACTG]+)',
    "origDna"     : r'(?P<origDna>[actgACTG])',
    "origDnas"     : r'(?P<origDnas>[actgACTG]+)',
    "mutDna"      : r'(?P<mutDna>[actgACTGfs])',  # tolerate "fs"
    "fs"          : r'(?P<fs>(fs\*?[0-9]*)|fs\*|fs|)?',
    "intron"      : r'(?P<intron>[1-9][0-9]*)',
    "rightArrow"  : r'(-*>|\u2192|-?&gt;|r|R|4|\ufb02)',
    "sp"          : r'\u00a0| |'
    }
    regexTab = join(mutDataDir, "regex.txt")
    logger.info("Parsing regexes from %s" % regexTab)
    regexList = []
    counts = defaultdict(int)
    for row in maxCommon.iterTsvRows(regexTab, commentPrefix="#"):
        logger.info("Translating %s" % row.pat)
        patName = row.patName
        if patName == "":
            patName = row.pat
        patFull = row.pat.format(**replDict)
        logger.info("full pattern is %s" % patFull)
        flags = 0
        if "Long}" in row.pat:
            flags = re.IGNORECASE
            logger.info("ignoring case for this pattern")
        patComp = re.compile(patFull, flags=flags)
        if row.isCoding == "True":
            isCoding = True
        elif row.isCoding == "False":
            isCoding = False
        else:
            logger.info("Skipping regex. Invalid value for isCoding: "+row.isCoding)
            continue
        regexList.append((row.seqType, row.mutType, isCoding, patName, patComp))
        counts[(row.seqType, row.mutType)] += 1

    for regexType, count in counts.iteritems():
            logger.info("regexType %s, found %d regexes" % (str(regexType), count))
    return regexList

def parseMatchRsId(match, patName):
    """ given a regular expression match object,
    return special mutation object for rsIds
    that includes the chromosome coordinates """
    groups = match.groupdict()
    mutType = "dbSnp"
    rsId = groups["rsId"]
    chrom, start, end = geneData.rsIdToGenome(rsId)

    if chrom == None:
        return None
    var = VariantDescription("dbSnp", "dbSnp", start, end, chrom, "rs" + rsId, None, None, origStr=match.group(0).strip())
    return var

def isBlacklisted(let1, pos, let2):
    " check if a string like T47D is blacklisted, like cell names or common chemical symbols "
    if (let1, pos, let2) in blackList:
        logger.debug("Variant %s,%d,%s is blacklisted" % (let1, pos, let2))
        return True
    if let1 == "H" and pos < 80 and let2 in "ACDE":
        logger.debug("Variant %s,%d,%s looks like chemical symbol" % (let1, pos, let2))
        return True
    if let1 == "C" and pos < 80 and let2 in "H":
        logger.debug("Variant %s,%d,%s looks like chemical symbol" % (let1, pos, let2))
        return True
    return False

def parseMatchSplicing(match, patName, seqType):
    # dna splicing        {sep}c\.{pos}{plusMinus}{offset}{origDna}>{mutDna}
    groups = match.groupdict()
    seqStart = int(groups["pos"])
    seqEnd = seqEnd = seqStart + 1
    plusMinus = groups["plusMinus"]
    offset = int(groups["offset"])
    if plusMinus == "+":
        pass
    elif plusMinus == "-":
        offset *= -1
    else:
        assert False
    origSeq = groups["origDna"]
    mutSeq = groups["mutDna"]
    var = VariantDescription("splicing", seqType, seqStart, seqEnd, origSeq, mutSeq, origStr=match.group(0).strip(), offset=offset)
    return var

def parseMatchSub(match, patName, seqType, isCoding):
    " given a regular expression match object, return mutation and mention objects "
    groups = match.groupdict()
    # grab long and short versions of amino acid
    offset = 0
    if not isCoding:
        if "plusMinus" in groups:
            if groups["plusMinus"] == "+":
                offset = int(groups["offset"])
            if groups["plusMinus"] == "-":
                offset = (-1)*int(groups["offset"])

    if "origAaShort" in groups:
        origSeq = groups["origAaShort"]
    if "origAaLong" in groups:
        origSeq = threeToOneLower[groups["origAaLong"].lower()]

    if "mutAaShort" in groups:
        mutSeq = groups["mutAaShort"]
    if "mutAaLong" in groups:
        mutSeq = threeToOneLower[groups["mutAaLong"].lower()]

    if "origDna" in groups:
        origSeq = groups["origDna"]
    if "mutDna" in groups:
        mutSeq = groups["mutDna"]

    mutSeq = mutSeq.upper()
    origSeq = origSeq.upper()

    if "fromPos" in groups:
        pos = int(groups["fromPos"])
        seqStart = pos

    if "toPos" in groups:
        seqEnd = int(groups["toPos"])
    else:
        pos = int(groups["pos"])
        if isBlacklisted(origSeq, pos, mutSeq):
            return None
        seqStart = pos
        seqEnd = pos + 1

    var = VariantDescription("sub", seqType, seqStart, seqEnd, origSeq, mutSeq, \
                             offset=offset, origStr=match.group(0).strip())
    return var

def parseMatchDel(match, patName, seqType, isCoding):
    " given a regular expression match object, return mutation and mention objects "
    groups = match.groupdict()

    offset = 0
    if not isCoding:
        if "plusMinus" in groups:
            if groups["plusMinus"] == "+":
                offset = int(groups["offset"])
            if groups["plusMinus"] == "-":
                offset = (-1)*int(groups["offset"])

    if "fromPos" in groups:
        pos = int(groups["fromPos"])
        seqStart = pos

    if "toPos" in groups:
        seqEnd = int(groups["toPos"]) + 1
    else:
        pos = int(groups["pos"])
        seqStart = pos
        seqEnd = pos + 1

    if "origAasShort" in groups:
        origSeq = groups["origAasShort"]
    if "origAasLong" in groups:
        origSeq = threeToOneLower[groups["origAasLong"].lower()]
    if "origDnas" in groups:
        origSeq = groups["origDnas"]
    if "origDna" in groups:
        origSeq = groups["origDna"]
    if "origAaShort" in groups:
        origSeq = groups["origAaShort"]
    elif "origAaLong" in groups:
        origSeq = threeToOneLower[groups["origAaLong"].lower()]
    origSeq = origSeq.upper()

    var = VariantDescription("del", seqType, seqStart, seqEnd, origSeq, None, \
                             offset=offset, origStr=match.group(0).strip())
    return var

def parseMatchIns(match, patName, seqType, isCoding):
    " given a regular expression match object, return mutation and mention objects "
    groups = match.groupdict()

    offset = 0
    if not isCoding:
        if "plusMinus" in groups:
            if groups["plusMinus"] == "+":
                offset = int(groups["offset"])
            if groups["plusMinus"] == "-":
                offset = (-1)*int(groups["offset"])

    if "fromPos" in groups:
        pos = int(groups["fromPos"])
        seqStart = pos

    if "toPos" in groups:
        seqEnd = int(groups["toPos"])
    else:
        pos = int(groups["pos"])
        seqStart = pos
        seqEnd = pos + 1

    mutSeq = None
    if "mutAasShort" in groups:
        mutSeq = groups["mutAasShort"]
    if "mutAasLong" in groups:
        mutSeq = threeToOneLower[groups["mutAasLong"].lower()]
    if "dnas" in groups:
        mutSeq = groups["dnas"]
    logger.debug("match: %s" % match.group(0))
    logger.info(str([g for g in groups]))

    if mutSeq is not None:
        mutSeq = mutSeq.upper()

    var = VariantDescription("ins", seqType, seqStart, seqEnd, None, mutSeq, \
                             offset=offset, origStr=match.group(0).strip())
    return var

def parseMatchDup(match, patName, seqType, isCoding):
    groups = match.groupdict()

    offset = 0
    if not isCoding:
        if "plusMinus" in groups:
            if groups["plusMinus"] == "+":
                offset = int(groups["offset"])
            if groups["plusMinus"] == "-":
                offset = (-1)*int(groups["offset"])

    if "origDna" in groups:
        origSeq = groups["origDna"]
    elif "origDnas" in groups:
        origSeq = groups["origDnas"]
    else:
        assert False

    if "pos" in groups:
        pos = int(groups["pos"])
        seqStart = pos
        seqEnd = seqStart + 1
    elif "fromPos" in groups:
        assert "toPos" in groups
        seqStart = int(groups["fromPos"])
        seqEnd = int(groups["toPos"])
        # it's just cause some people don't like basic arithmetic, or they're trying to be inclusive
        if seqEnd - seqStart != len(origSeq) and seqEnd - seqStart == len(origSeq) - 1:
            seqEnd += 1
    else:
        assert False

    # duplication ... in the old Indian fashion:
    mutSeq = origSeq + origSeq
    # ! duplication ... in the Assyro-Babylonian fashion:
    # ! mutSeq = origSeq * 2

    var = VariantDescription("dup", seqType, seqStart, seqEnd, origSeq, mutSeq, \
                             offset=offset, origStr=match.group(0).strip())
    return var

def isOverlapping(match, exclPos):
    posSet = set(range(match.start(), match.end()))
    if len(posSet.intersection(exclPos)) != 0:
        logger.debug("regex overlaps an excluded position (gene?)")
        return True
    return False

def findVariantDescriptions(text, exclPos=set()):
    """ put mutation mentions from document together into dicts indexed by normal form
        return dict of "prot"|"dna"|"dbSnp" -> list of (VariantDescription, list of Mention)
        uses global variable "regexes", see loadDb()

    >>> findVariantDescriptions("The R71G BRCA1 mutation is really a p.R71G mutation")
    {'prot': [(VariantDescription(mutType=u'sub',seqType=u'prot',seqId=u'None',geneId=u'',start=u'70',end=u'71',origSeq=u'R',mutSeq=u'G'), [Mention(patName=u'{sep}p\\\\.\\\\(?{origAaShort}{pos}{mutAaShort}{fs}', start=35, end=42), Mention(patName=u'{sep}{origAaShort}{pos}{mutAaShort}', start=3, end=8)])]}
    """
    if regexes == None:
        loadDb()

    exclPos = set(exclPos)
    varMentions = defaultdict(list)
    varDescObj = {}
    for seqType, mutType, isCoding, patName, pat in regexes:
        for match in pat.finditer(text):
            logger.debug("Match: Pattern %s, text %s" % (patName, match.groups()))
            if isOverlapping(match, exclPos):
                logger.debug("Overlapping with exclPos")
                continue
            if mutType == "sub":
                variant = parseMatchSub(match, patName, seqType, isCoding)
            elif mutType == "dbSnp":
                variant = parseMatchRsId(match, patName)
            elif mutType == "del":
                variant = parseMatchDel(match, patName, seqType, isCoding)
            elif mutType == "ins":
                variant = parseMatchIns(match, patName, seqType, isCoding)
            elif mutType == "dup":
                variant = parseMatchDup(match, patName, seqType, isCoding)
            else:
                logger.debug("Ignoring match %s; don't know how to handle" % match.groups())
                continue
            if variant == None:
                continue

            mention = makeMention(match, patName)
            varDescObj[variant.getName()] = variant
            varMentions[variant.getName()].append(mention)
            debugSnip = pubAlg.getSnippet(text, mention.start, mention.end, maxContext=60)
            logger.debug("Found Variant: %s, snippet %s" % (str(variant), debugSnip))

    # convert to dict of "prot"|"dna"|"dbSnp" -> list (variant, mentions)
    variants = {}
    variants["prot"] = []
    variants["dna"] = []
    variants["dbSnp"] = []
    variants["intron"] = []

    for varName, mentions in varMentions.iteritems():
        variant = varDescObj[varName]
        variants[variant.seqType].append((variant, mentions))
    variants = dict(variants)
    return variants

def makeHgvsStr(seqType, seqId, origSeq, pos, mutSeq, offset):
    if seqType == "prot":
        desc = "%s:p.%s%d%s" % (seqId, oneToThree.get(origSeq, "None"), pos, oneToThree.get(mutSeq, "None"))
    elif seqType == "cds":
        desc = "%s:c.%d%s>%s" % (seqId, pos, origSeq, mutSeq)
    elif seqType == "rna":
        desc = "%s:r.%d%s>%s" % (seqId, pos, origSeq, mutSeq)
    elif seqType == "dna":
        desc = "%s:c.%d%s>%s" % (seqId, pos, origSeq, mutSeq)
    return desc

def firstDiffNucl(str1, str2, maxDiff=1):
    """Return first pos and all letters where strings differ. Returns None if more than maxDiff chars are different"""
    assert(len(str1) == len(str2))
    if str1 == str2:
        return None
    diffCount = 0
    i = 0
    diffPos = []
    diffCh1 = []
    diffCh2 = []

    for ch1, ch2 in zip(str1, str2):
        if ch1 != ch2:
            diffCount += 1
            diffCh1.append(ch1)
            diffCh2.append(ch2)
            diffPos.append(i)
        i += 1

    if diffCount > maxDiff:
        return None
    elif diffCount == 1:
        return (diffPos[0], diffCh1[0], diffCh2[0])
    elif diffCount == 2 and diffPos[0] + 1 == diffPos[1]:
        return (diffPos[0], "".join(diffCh1), "".join(diffCh2))
    return None

def possibleDnaChanges(origAa, mutAa, origDna):
    """ figure out which nucleotides were possibly mutated by an amino acid change
    will only look for single-bp mutations
    returns list of: position of nucleic acid, original and new basepair
    >>> possibleDnaChanges("V", "V", "GTA")
    [(2, 'A', 'T'), (2, 'A', 'C'), (2, 'A', 'G')]
    >>> possibleDnaChanges("V", "I", "GTA")
    [(0, 'G', 'A')]
    >>> possibleDnaChanges("G", "G", "GGC")
    [(2, 'C', 'T'), (2, 'C', 'G'), (2, 'C', 'A')]
    """
    maxDiff = 1
    if allowTwoBpVariants:
        maxDiff = 2

    origDna = origDna.upper()
    ret = set()
    mutDnas = backTrans(mutAa)
    logger.debug("Looking for possible DNA change. Aa change %s -> %s, original dna %s" % (origAa, mutAa, origDna))
    for mutDna in mutDnas:
        diffTuple = firstDiffNucl(origDna, mutDna, maxDiff)
        if diffTuple != None:
            ret.add(diffTuple)
            logger.debug("found possible mutated DNA: %s" % (mutDna))

    if len(ret) == 0:
        logger.debug("No possible DNA change found (max %d bp change)." % maxDiff)

    return list(ret)

def newToOldRefseqs(accs):
    """ given a list of new accessions return all previous versions
    >>> newToOldRefseqs(["NM_000325.5"])
    ['NM_000325.1', 'NM_000325.2', 'NM_000325.3', 'NM_000325.4']
    """
    oldAccs = []
    for newAcc in accs:
        prefix, suffix = newAcc.split(".")
        version = int(suffix) - 1
        if version != 0:
            oldVersions = range(0, version)
            oldVersions = [ov + 1 for ov in oldVersions]
            for oldVersion in range(0, version):
                oldVersion = oldVersion + 1
                oldAcc = prefix + "." + str(oldVersion)
                oldAccs.append(oldAcc)
    return oldAccs

def backTrans(aa):
    """ back translate protein to all nucleotide strings
    Returns the back-translated nucleotide sequences for a protein and codon
    table combination.
    copied from http://www.biostars.org/p/3129/
    >>> protein = 'FVC'
    >>> len(backTrans(protein))
    16
    >>> backTrans('CD')
    ['TGTGAT', 'TGCGAT', 'TGTGAC', 'TGCGAC']
    """
    # create initial sequences == list of codons for the first amino acid
    sequences = [codon for codon in aaToDna[aa[0]]]
    for amino_acid in aa[1:]:
        # add each codon to each existing sequence replacing sequences
        # leaves (num_codons * num_sequences) for next amino acid
        to_extend = sequences
        sequences = []
        for codon in aaToDna[amino_acid]:
            for sequence in to_extend:
                sequence += codon
                sequences.append(sequence)
    return sequences

def translate(dna):
    " return the aa translation of a dna seq "
    aaSeq = []
    for i in range(0, len(dna), 3):
        codon = dna[i:i + 3].upper()
        aa = dnaToAa[codon]
        aaSeq.append(aa)
    return "".join(aaSeq)

def dnaAtCodingPos(refseqId, start, end, expectAa):
    """
    get nucleotide at CODING position in refseqId, check against expected aa
    also return positions on cdna
    """
    logger.debug("Paranoia check: making sure that codons from %d-%d in %s correspond to %s" %
        (start, end, refseqId, expectAa))
    cdsStart = geneData.getCdsStart(str(refseqId))
    nuclStart = cdsStart + (3 * start)
    nuclEnd = nuclStart + 3 * (end - start)
    cdnaSeq = geneData.getSeq(refseqId)
    if cdnaSeq == None:
        logger.warn("Could not find seq %s (update diff between UCSC/NCBI maps?)" % refseqId)
        return None, None, None
    nuclSeq = cdnaSeq[nuclStart:nuclEnd]
    foundAa = translate(nuclSeq)
    logger.debug("CDS start is %d, nucl pos is %d, codon is %s" % (cdsStart, nuclStart, nuclSeq))
    if not doShuffle:
        assert(foundAa == expectAa)
    return nuclSeq, nuclStart, nuclEnd

def mapToCodingAndRna(protVars):
    """ given ref protein positions and refseq proteinIds, try to figure out the nucleotide
    changes on the refseq cdna sequence and add these to the variant object
    """
    codVars = []
    rnaVars = []
    for protVar in protVars:
        transId = geneData.getRefSeqId(protVar.seqId)
        if transId == None:
            logger.error("could not resolve refprot to refseq. This is due to a difference between "
                    "UniProt and Refseq updates. Skipping this protein.")
            continue

        pos = protVar.start - 1
        origDnaSeq, cdnaStart, cdnaEnd = dnaAtCodingPos(transId, pos, \
            pos + len(protVar.origSeq), protVar.origSeq)
        if origDnaSeq == None:
            return None, None

        if protVar.mutType == "del" and protVar.mutSeq is None:
            # the salomonian solution ... I don't exactly know where this deletion starts and ends,
            # at least some papers (25278557) do a poor job of telling you what's going on, so just settling
            # on this position for the deletion
            cdStart = 3 * protVar.start
            cdEnd = cdStart + 3 * len(protVar.origSeq)
            codVar = VariantDescription(protVar.mutType, "cds", cdStart, cdEnd, origDnaSeq, None, transId, origStr=protVar.origStr)
            codVars.append(codVar)

            cdnaNuclStart = cdnaStart
            cdnaNuclEnd = cdnaNuclStart + 3 * len(protVar.origSeq)
            rnaVar = VariantDescription(protVar.mutType, "rna", cdnaNuclStart, cdnaNuclEnd, \
                origDnaSeq, None, transId, origStr=protVar.origStr)
            rnaVars.append(rnaVar)
        else:
            possChanges = possibleDnaChanges(protVar.origSeq, protVar.mutSeq, origDnaSeq)
            for relPos, oldNucl, newNucl in possChanges:
                cdStart = 3 * (protVar.start-1) + relPos + 1
                cdEnd = cdStart + len(oldNucl)
                codVar = VariantDescription(protVar.mutType, "cds", cdStart, cdEnd, oldNucl, newNucl, transId, origStr=protVar.origStr)
                codVars.append(codVar)

                cdnaNuclStart = cdnaStart + relPos
                cdnaNuclEnd = cdnaNuclStart + len(newNucl)
                rnaVar = VariantDescription(protVar.mutType, "rna", cdnaNuclStart, cdnaNuclEnd, \
                    oldNucl, newNucl, transId, origStr=protVar.origStr)
                rnaVars.append(rnaVar)

    return codVars, rnaVars

def bedToRsIds(beds):
    " return a comma-sep string of rsIds given bed (12-tuple) features "
    ret = []
    for bed in beds:
        chromCoord = bed[0], bed[1], bed[2]
        snpId = geneData.lookupDbSnp(*chromCoord)
        if snpId is None:
            logger.debug("Chromosome location %s does not map to any dbSNP" % str(chromCoord))
            ret.append("na")
        else:
            logger.debug("Chromosome location %s corresponds to dbSNP %s " % (chromCoord, snpId))
            ret.append(snpId)
    return ret

def mentionsFields(mentions, text):
    " convert the mention objects to something that fits into a tab-sep file "
    mutStarts = []
    mutEnds = []
    snippets = []
    patNames = []
    texts = []
    for m in mentions:
        mutStarts.append(str(m.start))
        mutEnds.append(str(m.end))
        snippets.append(pubAlg.getSnippet(text, m.start, m.end).replace("|", " "))
        patNames.append(m.patName)
        texts.append(text[m.start:m.end].strip("() -;,."))
    return mutStarts, mutEnds, patNames, snippets, texts

def unmappedRsVarsToFakeVariants(varMentionsList, mappedRsIds, text):
    muts = []
    for dbSnpId, mentions in varMentionsList:
        if not dbSnpId in mappedRsIds:
            muts.append(SeqVariantData(seqType="dbSnp", mentions=mentions, text=text, patType="dbSnp"))
    return muts

def ungroundedMutToFakeSeqVariant(variant, mentions, text):
    """ convert mutations that could not be grounded to "fake" variants
        that are not located on any sequence but are easy to write to a file
    """
    # muts = []
    # for mut, mentions in ungroundedMuts:
    # muts.append(SeqVariantData(seqType=mut.seqType, mentions=mentions, text=text))
    var = SeqVariantData(seqType=variant.seqType, mentions=mentions, text=text)
    return var

def isSeqCorrect(seqId, variant, insertion_rv):
    """ check if wild type sequence in protein corresponds to mutation positions or to insertion_rv:
    if the original variant is not checkable because the reference sequence is not given
    (for example in insertions), then return insertion_rv. If insertion_rv is True, then eventually
    all uncheckable variants will be mapped to all candidate genes. If insertion_rv is False, then
    eventually all uncheckable variants will be dropped."""
    if seqId.startswith("NR_"):
        logger.info("Skipping noncoding sequence ID %s" % seqId)
        return False

    if variant.mutType == "ins" and not variant.origSeq:
        return insertion_rv

    vStart = variant.start - 1  # uniprot is 1-based, we are 0-based
    vEnd = variant.end - 1
    seq = geneData.getSeq(seqId)
    logger.info("vStart: %d" % vStart)
    logger.info("vEnd: %d" % vEnd)
    if seq == None:
        # uniprot sometimes uses other species as support
        logger.debug("sequence %s is not human or not available" % seqId)
        return False
    logger.info("len(seq): %d" % len(seq))

    if not vEnd <= len(seq):
        logger.debug("sequence %s is too short" % seqId)
        return False

    if doShuffle:
        s = list(seq)
        random.shuffle(s)
        seq = "".join(s)

    if variant.seqType == "dna":
        cdsStart = geneData.getCdsStart(seqId)
    else:
        cdsStart = 0
    genomeSeq = seq[vStart + cdsStart:vEnd + cdsStart].upper()
    if genomeSeq == variant.origSeq.upper():
        logger.debug("Seq match: Found %s at pos %d-%d in seq %s" % \
            (genomeSeq, vStart, vEnd, seqId))
        return True
    else:
        surroundingSeq = seq[vStart + cdsStart - 5:vEnd + 5 + cdsStart].upper()
        logger.debug("No seq match: Need %s, but found %s at pos %d-%d in seq %s (surrounding: %s) (vStart: %d, vEnd: %d, cdsStart: %d)" % \
            (variant.origSeq, genomeSeq, vStart, vEnd, seqId, surroundingSeq, vStart, vEnd, cdsStart))
        return False

def hasSeqAtPos(seqIds, variant, insertion_rv):
    " check a list of IDs return those with a wild-type sequence at a position "
    if seqIds == None:
        return []
    foundIds = []
    for seqId in seqIds:
        if isSeqCorrect(seqId, variant, insertion_rv):
            foundIds.append(seqId)
    return foundIds

def checkVariantAgainstSequence(variant, entrezGene, sym, insertion_rv, seqDbs=["refseq"]):
    """ given a variant and a gene ID,
    try to resolve gene to transcript sequence via  various protein databases
    and check if they have matches for the wildtype aa at the right position
    seqDbs can be any of "refseq", "oldRefseq", "uniprot", "genbank"
    - variant is a namedtuple with VariantFields defined above
    - entrezGene has to be a number as a string or a list of numbers separated by "/"
    - sym is only used for the logger system
    - insertion_rv: if the original variant is not checkable because the reference sequence
    is not given (for example in insertions), then return insertion_rv. If insertion_rv is
    True, then eventually all uncheckable variants will be mapped to all candidate genes.
    If insertion_rv is False, then eventually all uncheckable variants will be dropped.
    """
    entrezGene = str(entrezGene)
    for entrezGene in entrezGene.split("/"):
        entrezGene = int(entrezGene)
        logger.debug("Trying to ground %s to entrez gene %s / %s" % (str(variant), entrezGene, sym))
        for db in seqDbs:
            if variant.seqType == "prot":
                seqIds = geneData.entrezToProtDbIds(entrezGene, db)
            elif variant.seqType == "dna" or variant.seqType == "intron":
                seqIds = geneData.entrezToCodingSeqDbIds(entrezGene, db)
            else:
                logger.debug("variant is neither DNA nor prot variant, but %s instead" % variant.seqType)
                continue
            if len(seqIds) == 0:
                continue
            foundSeqIds = hasSeqAtPos(seqIds, variant, insertion_rv)
            if len(foundSeqIds) != 0:
                return db, foundSeqIds
    return None, []

def rewriteToRefProt(variant, protIds):
    " create new VariantDescriptions, one for each protein Id "
    varList = []
    for protId in protIds:
        varNew = copy.copy(variant)
        varNew.seqId = protId
        varList.append(varNew)
    return varList

def mapToRefProt(db, variant, protIds):
    " map protein position from some DB to refseq proteins"
    logger.debug("Mapping original variant to refprot: %s" % str(variant))
    mappedVars = []
    newVar = None
    for protId in protIds:
        psls = geneData.getProteinPsls(db, protId)
        for psl in psls:
            newVar = pslMapVariant(variant, psl)
            if newVar == None:
                logger.warn("Cannot map a variant to refprot")
                continue
            # some variants are in uniprot but cannot be mapped to refseq at all
            if not hasSeqAtPos([newVar.seqId], newVar):
                logger.warn("variant %s is unique to db %s" % (newVar, db))
                continue
            mappedVars.append(newVar)
    logger.debug("Mapped to refprot variants: %s" % str(newVar))
    return mappedVars

def pslMapVariant(variant, psl):
    " map variant through psl on target, given query position, and create a new variant "
    maker = pslMapBed.PslMapBedMaker()
    maker.mapQuery(psl, variant.start, variant.end)
    bed = maker.getBed()
    if bed == None:
        return None

    varNew = copy.deepcopy(variant)
    varNew.seqId = bed[0]
    varNew.start = int(bed[1])
    varNew.end = int(bed[2])
    return varNew

def mapToGenome(rnaVars, bedName):
    " map to genome from refseq, remove duplicate results, return as 12-tuple (=BED) "
    maker = pslMapBed.PslMapBedMaker()
    beds = []
    for rnaVar in rnaVars:
        logger.debug("Mapping rnaVar %s:%d-%d (offset %d) to genome" % (rnaVar.seqId, rnaVar.start, rnaVar.end, rnaVar.offset))
        maker.clear()
        # get psl
        pslList = geneData.getRefseqPsls(rnaVar.seqId)
        if len(pslList) > 1:
            logger.warn("refSeq %s maps to multiple places, using only first one" % rnaVar.seqId)
        if len(pslList) == 0:
            logger.warn("No mapping for %s, skipping variant" % rnaVar.seqId)
            continue
        mapPsl = pslList[0]

        # map rna var through psl
        start = rnaVar.start
        end = rnaVar.end
        maker.mapQuery(mapPsl, start, end)
        bed = maker.getBed(name=bedName)
        if bed == None:
            logger.debug("found mapping psl but nothing was mapped")
            continue
        bed.append(rnaVar.origStr)
        bed[1] = str(int(bed[1]) + rnaVar.offset)
        bed[2] = str(int(bed[2]) + rnaVar.offset)
        bed[6] = str(int(bed[6]) + rnaVar.offset)
        bed[7] = str(int(bed[7]) + rnaVar.offset)

        logger.debug("Got bed: %s" % str(bed))
        beds.append(bed)
    return beds

def getSnpMentions(mappedRsIds, varList):
    """ find all variants + their mentions with any of the mapped rsIds
        returns dict rsId -> list of mentions
    """
    if len(varList) == 0:
        return {}

    mappedRsIds = set(mappedRsIds)
    if "na" in mappedRsIds:
        mappedRsIds.remove("na")
    if len(mappedRsIds) == 0:
        return {}

    res = defaultdict(list)
    for var, mentions in varList:
        rsId = var.origSeq
        if rsId in mappedRsIds:
            res[rsId].extend(mentions)
    return res

def findClosestGeneMention(mentions, entrezGenes):
    """
    Find which gene was mentioned in closest proximity to the give variant mention
    """
    closestGene = None
    closestDistance = float('inf')
    for mention in mentions:
        logger.error(str(mentions))
        for entrezGene in entrezGenes:
            gene = entrezGenes[entrezGene]
            geneMentions = sum([sum((x[1] for x in gene[key]), []) for key in gene], [])
            for geneMention in geneMentions:
                distance = min(abs(int(geneMention[0])-int(mention.start)), abs(int(geneMention[1])-int(mention.end)))
                if distance < closestDistance:
                    closestDistance = distance
                    closestGene = entrezGene

    return closestGene

def groundVariant(docId, text, variant, mentions, snpMentions, entrezGenes, insertion_rv):
    """
    ground mutations onto genes and return a tuple of:
    (list of grounded SeqVariantData objects, list of ungrounded SeqVariantData,
    list of genome coordinate tuples in BED format)

    >>> text = "The R71G BRCA1 mutation"
    >>> vDesc = VariantDescription(mutType=u'sub',seqType=u'prot',seqId=u'None',geneId=u'',start=u'70',end=u'71',origSeq=u'R',mutSeq=u'G')
    >>> mentions = [Mention(patName=u'{sep}{origAaShort}{pos}{mutAaShort}', start=3, end=8)]
    >>> groundVariant("0", text, vDesc, mentions, [], ['672'])
    ([SeqVariantData(chrom=u'',start=u'',end=u'',varId=u'0',inDb=u'',patType=u'sub',hgvsProt=u'NP_009230.2:p.Arg71Gly|NP_009225.1:p.Arg71Gly|NP_009229.2:p.Arg71Gly|NP_009231.2:p.Arg71Gly',hgvsCoding=u'NM_007299.3:c.211A>G|NM_007294.3:c.211A>G|NM_007298.3:c.211A>G|NM_007300.3:c.211A>G',hgvsRna=u'NM_007299.3:r.405A>G|NM_007294.3:r.443A>G|NM_007298.3:r.230A>G|NM_007300.3:r.443A>G',comment=u'',rsIds=u'rs80357382|rs80357382|rs80357382|rs80357382',protId=u'',texts=u'R71G',rsIdsMentioned=u'',dbSnpStarts=u'',dbSnpEnds=u'',geneSymbol=u'BRCA1',geneType=u'entrez',entrezId=u'672',geneStarts=u'',geneEnds=u'',seqType=u'prot',mutPatNames=u'{sep}{origAaShort}{pos}{mutAaShort}',mutStarts=u'3',mutEnds=u'8',mutSnippets=u'The<<< R71G>>> BRCA1 mutation',geneSnippets=u'',dbSnpSnippets=u'')], None, [['chr17', '41258473', '41258474', '0', '1', '-', '41258473', '41258474', '0', '1', '1', '0', u'NM_007299.3:r.405A>G', u'NP_009230.2:p.Arg71Gly,NP_009225.1:p.Arg71Gly,NP_009229.2:p.Arg71Gly,NP_009231.2:p.Arg71Gly'], ['chr17', '41258473', '41258474', '0', '1', '-', '41258473', '41258474', '0', '1', '1', '0', u'NM_007294.3:r.443A>G', u'NP_009230.2:p.Arg71Gly,NP_009225.1:p.Arg71Gly,NP_009229.2:p.Arg71Gly,NP_009231.2:p.Arg71Gly'], ['chr17', '41258473', '41258474', '0', '1', '-', '41258473', '41258474', '0', '1', '1', '0', u'NM_007298.3:r.230A>G', u'NP_009230.2:p.Arg71Gly,NP_009225.1:p.Arg71Gly,NP_009229.2:p.Arg71Gly,NP_009231.2:p.Arg71Gly'], ['chr17', '41258473', '41258474', '0', '1', '-', '41258473', '41258474', '0', '1', '1', '0', u'NM_007300.3:r.443A>G', u'NP_009230.2:p.Arg71Gly,NP_009225.1:p.Arg71Gly,NP_009229.2:p.Arg71Gly,NP_009231.2:p.Arg71Gly']])
    """
    groundedMuts = []
    ungroundedMuts = []
    mappedRsIds = []

    allBeds = []
    logger.debug("Grounding mutation %s onto genes %s" % (variant, entrezGenes))
    groundSuccess = False
    # try all entrez genes in article
    for entrezGene in entrezGenes:
        geneSym = geneData.entrezToSym(entrezGene)
        if not geneSym:
            logger.warn("No symbol for entrez gene %s. Skipping gene." % str(entrezGene))
            continue
        db, seqIds = checkVariantAgainstSequence(variant, entrezGene, geneSym, insertion_rv)

        if len(seqIds) != 0:
            # we found a sequence hit
            varId = str(docId)
            comment = ""
            if variant.seqType == "prot":
                if db == "refseq":
                    protVars = rewriteToRefProt(variant, seqIds)
                else:
                    assert(False)

                # map variants to coding and rna sequence coordinates
                codVars, rnaVars = mapToCodingAndRna(protVars)
                if codVars == None:
                    continue
                beds = mapToGenome(rnaVars, varId)
                logger.info("protVars: %s" % str(protVars))
                logger.info("codVars: %s" % str(codVars))
                logger.info("rnaVars: %s" % str(rnaVars))
                logger.info("beds: %s" % str(beds))
            elif variant.seqType == "dna":
                protVars = []
                codVars = []
                rnaVars = []
                for seqId in seqIds:
                    cVariant = copy.copy(variant)
                    cVariant.seqId = seqId
                    codVars.append(cVariant)
                    cdsStart = geneData.getCdsStart(seqId)
                    rVariant = copy.copy(variant)
                    # Johannes: not sure why -1, but so be it ... these wacko off by one errors are not worth hunting down to the original source lol
                    rVariant.start = variant.start + cdsStart - 1
                    rVariant.end = variant.end + cdsStart - 1
                    rVariant.seqId = seqId
                    rnaVars.append(rVariant)
                beds = mapToGenome(rnaVars, varId)
            elif variant.seqType == "intron":
                groundSuccess = False
                break
            else:
                assert False, "can only ground prot and dna variants"
            # add all relevant dbSnp IDs from the document to this variant
            varRsIds = bedToRsIds(beds)
            # mentionedDbSnpVars = getSnpMentions(varRsIds, mutations["dbSnp"])
            mentionedDbSnpVars = getSnpMentions(varRsIds, snpMentions)
            mappedRsIds.extend(mentionedDbSnpVars.keys())

            groundedVar = SeqVariantData(varId, protVars, codVars, rnaVars, comment, beds, \
                entrezGene, geneSym, varRsIds, mentionedDbSnpVars, mentions, text, seqType=variant.seqType)

            # isInDb = dbAnnots.addCheckVariant(groundedVar)
            groundedMuts.append(groundedVar)
            allBeds.extend(beds)
            groundSuccess = True
        else:
            logger.debug("Len of protIds is 0")

    # if not groundSuccess:
        # ungroundedMuts.append((variant,  mentions))

    # if not groundSuccess:
        # ungroundVar = ungroundedMutToFakeSeqVariant(variant, mentions, text)
    ungroundVar = None
    if not groundSuccess:
        ungroundVar = SeqVariantData(seqType=variant.seqType, mentions=mentions, text=text)

    unmappedRsVars = unmappedRsVarsToFakeVariants(snpMentions, mappedRsIds, text)
    # ungroundVarData.extend(unmappedRsVars)
    return groundedMuts, ungroundVar, allBeds

def groundSymbolVariant(geneSym, protDesc):
    """ simple interface. given a gene symbol and a AA, return the genome beds
    >>> groundProtVariant("BRAF", "V600E")
    ([['chr7', '140453135', '140453136', 'BRAF:V600E', '1', '-', '140453135', '140453136', '0', '1', '1', '0', u'NM_004333.4:r.1860T>A', u'NP_004324.2:p.Val600Glu']], [VariantDescription(mutType=u'sub',seqType=u'cds',seqId=u'NM_004333.4',geneId=u'',start=u'1798',end=u'1801',origSeq=u'T',mutSeq=u'A')], [VariantDescription(mutType=u'sub',seqType=u'rna',seqId=u'NM_004333.4',geneId=u'',start=u'1859',end=u'1860',origSeq=u'T',mutSeq=u'A')])
    """
    varDesc = findVariantDescriptions(protDesc)
    if "prot" not in varDesc:
        return None
    variant = varDesc["prot"][0][0]
    entrezGenes = geneData.mapSymToEntrez(geneSym)
    # use the first entrez entry we find
    for entrezGene in entrezGenes:
        db, protIds = checkVariantAgainstSequence(variant, str(entrezGene), geneSym, "")
        if len(protIds) != 0:
            break
    protVars = rewriteToRefProt(variant, protIds)
    codVars, rnaVars = mapToCodingAndRna(protVars)
    beds = mapToGenome(rnaVars, protVars)
    return beds, codVars, rnaVars

if __name__ == "__main__":
    logger.basicConfig(level=logger.DEBUG)
    import doctest
    doctest.testmod()
