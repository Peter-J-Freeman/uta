from __future__ import absolute_import, division, print_function, unicode_literals

import csv
import datetime
import gzip
import itertools
import logging
import time

from bioutils.coordinates import strand_pm_to_int, MINUS_STRAND
from bioutils.digests import seq_md5
from bioutils.sequences import reverse_complement
from multifastadb import MultiFastaDB
from sqlalchemy.orm.exc import NoResultFound
import psycopg2.extras
import uta_align.align.algorithms as utaaa

from .lru_cache import lru_cache

import uta
import uta.formats.exonset as ufes
import uta.formats.geneinfo as ufgi
import uta.formats.seqinfo as ufsi
import uta.formats.txinfo as ufti
import uta.parsers.geneinfo
import uta.parsers.seqgene

usam = uta.models                         # backward compatibility

logger = logging.getLogger(__name__)

#TODO: Schema qualify all queries/remove set search_path

def drop_schema(session, opts, cf):
    if session.bind.name == "postgresql" and usam.use_schema:
        session.execute(
            "set role {admin_role};".format(admin_role=cf.get("uta", "admin_role")))
        session.execute("set search_path = " + usam.schema_name)

        ddl = "drop schema if exists " + usam.schema_name + " cascade"
        session.execute(ddl)
        session.commit()
        logger.info(ddl)


def create_schema(session, opts, cf):
    """Create and populate initial schema"""
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    if session.bind.name == "postgresql" and usam.use_schema:
        session.execute("create schema " + usam.schema_name)
        session.execute("set search_path = " + usam.schema_name)
        session.commit()

    usam.Base.metadata.create_all(session.bind)
    session.add(usam.Meta(key="schema_version", value=usam.schema_version))
    session.add(
        usam.Meta(key="created on", value=datetime.datetime.now().isoformat()))
    session.add(usam.Meta(key="created by", value=uta.__version__))
    session.add(usam.Meta(
        key="license", value="CC-BY-SA (http://creativecommons.org/licenses/by-sa/4.0/deed.en_US"))
    session.commit()
    logger.info("created schema")


def load_sql(session, opts, cf):
    """Create views"""
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    for fn in opts["FILES"]:
        logger.info("loading " + fn)
        session.execute(open(fn).read())
    session.commit()


def load_origin(session, opts, cf):
    """Add/merge data origins

    """

    def _none_if_empty(s):
        return None if s == "" else s

    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    orir = csv.DictReader(open(opts["FILE"]), delimiter=b'\t')
    for rec in orir:
        ori = usam.Origin(name=rec["name"],
                          descr=_none_if_empty(rec["descr"]),
                          url=_none_if_empty(rec["url"]),
                          url_ac_fmt=_none_if_empty(rec["url_ac_fmt"]),
                          )
        u_ori = session.query(usam.Origin).filter(
            usam.Origin.name == rec["name"]).first()
        if u_ori:
            ori.origin_id = u_ori.origin_id
            session.merge(ori)
        else:
            session.add(ori)
        logger.info("Merged {ori.name} ({ori.descr})".format(ori=ori))
    session.commit()


def load_seqinfo(session, opts, cf):
    """load Seq entries with accessions from fasta file
    """

    # TODO: load_seqinfo is horrifically slow (via sqlalchemy). There
    # must be a better way.

    update_period = 100

    # TODO: Don't store sequences in UTA
    # load sequences up to max_len in size
    # 2e6 was chosen empirically based on sizes of NMs, NGs, NWs, NTs, NCs
    max_len = int(2e6)


    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    n_rows = len(gzip.open(opts["FILE"]).readlines()) - 1

    sir = ufsi.SeqInfoReader(gzip.open(opts["FILE"]))
    logger.info("opened " + opts["FILE"])

    mfdb = _get_mfdb(cf)

    _md5_seq_cache = {}
    def _upsert_seq(si):
        if si.md5 in _md5_seq_cache:
            return _md5_seq_cache[si.md5]

        u_seq = session.query(usam.Seq).filter(usam.Seq.seq_id == md5).first()
        if u_seq is None:
            seq = str(mfdb[si.ac]).upper()
            if int(si.len) != len(seq):
                raise RuntimeError("Expected a sequence of length {si.len} for {si.md5}; got length {len2} for {si.ac}; skipping".format(
                    si=si, len2=len(seq)))
            iseq = seq if len(seq) < max_len else None
            u_seq = usam.Seq(seq_id=si.md5, len=si.len, seq=iseq)
            session.add(u_seq)
        _md5_seq_cache[si.md5] = u_seq
        return _md5_seq_cache[si.md5]

    i_md5 = 0
    n_created = 0
    for md5, si_iter in itertools.groupby(sorted(sir, key=lambda si: si.md5),
                                          key=lambda si: si.md5):
        sis = list(si_iter)
    
        # if sequence doesn't exist in sequence table, make it
        # this is to satisfy a FK dependency, which should be reconsidered
        si = sis[0]
        try:
            u_seq = _upsert_seq(si)
        except RuntimeError as e:
            logger.exception(e)
            continue

        for si in sis:
            u_ori = session.query(usam.Origin).filter(
                usam.Origin.name == si.origin).one()
            u_seqanno = session.query(usam.SeqAnno).filter(
                usam.SeqAnno.origin_id == u_ori.origin_id,
                usam.SeqAnno.ac == si.ac).first()
            logger.debug("seq_anno({si.origin},{si.ac},{si.md5}) {st}".format(
                si=si, st="exists" if u_seqanno is not None else "doesn't exist"))
            if u_seqanno:
                # update descr, perhaps
                if u_seqanno.seq_id != si.md5:
                    raise RuntimeError("{si.origin}:{si.ac} for {si.md5}: accession already exists for {seq_id}".format(
                        si=si, seq_id=u_seqanno.seq_id))
                if si.descr and u_seqanno.descr != si.descr:
                    u_seqanno.descr = si.descr
                    session.merge(u_seqanno)
            else:
                # create the new annotation
                u_seqanno = usam.SeqAnno(origin_id=u_ori.origin_id, seq_id=si.md5,
                                         ac=si.ac, descr=si.descr)
                session.add(u_seqanno)
                n_created += 1

        i_md5 += 1
        if i_md5 % update_period == 1:
            logger.info("{n_created} annotations created/{i_md5} sequences seen ({p:.1f}%)/{n_rows} sequences total".format(
                n_created=n_created, i_md5=i_md5, n_rows=n_rows, md5=md5, p=i_md5 / n_rows * 100))
            session.commit()


def load_exonset(session, opts, cf):
    # exonsets and associated exons are loaded together

    update_period = 25

    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    known_tx = set([u_tx.ac
                    for u_tx in session.query(usam.Transcript)])

    n_rows = len(gzip.open(opts["FILE"]).readlines()) - 1
    esr = ufes.ExonSetReader(gzip.open(opts["FILE"]))
    logger.info("opened " + opts["FILE"])

    for i_es, es in enumerate(esr):
        key = (es.tx_ac, es.alt_ac, es.method)
    
        if es.tx_ac not in known_tx:
            # catch cases where UCSC has refseq transcripts not in UTA
            # Known causes:
            # - collecting UCSC data after NCBI data, and new refseq created
            # - refseq fails alignment criteria (see data/ncbi/ncbi-parse-gff.log)
            logger.warn("Could not load exon set {key}: unknown transcript {es.tx_ac}".format(
                es=es, key=key))
            continue

        existing = session.query(usam.ExonSet).filter(
                usam.ExonSet.tx_ac == es.tx_ac,
                usam.ExonSet.alt_ac == es.alt_ac,
                usam.ExonSet.alt_aln_method == es.alt_aln_method,
                )
        if existing.count() > 0:
            logger.warn("Replacing exon set with key {key}".format(key=key))
            existing.delete(synchronize=True)
            session.commit()

        u_es = usam.ExonSet(
            tx_ac=es.tx_ac,
            alt_ac=es.alt_ac,
            alt_aln_method=es.method,
            alt_strand=es.strand
        )
        session.add(u_es)

        exons = [map(int, ex.split(",")) for ex in es.exons_se_i.split(";")]
        exons.sort(reverse=int(es.strand) == MINUS_STRAND)
        for i_ex, ex in enumerate(exons):
            s, e = ex
            u_ex = usam.Exon(
                exon_set=u_es,
                start_i=s,
                end_i=e,
                ord=i_ex,
            )
            session.add(u_ex)

        session.commit()
        if i_es % update_period == 0 or i_es + 1 == n_rows:
            logger.info("{i_es}/{n_rows} {p:.1f}%: committed; (most recent exonset: {key})".format(
                i_es=i_es, n_rows=n_rows, p=(i_es + 1) / n_rows * 100, key=str(key)))


def load_geneinfo(session, opts, cf):
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    gir = ufgi.GeneInfoReader(gzip.open(opts["FILE"]))
    logger.info("opened " + opts["FILE"])

    for i_gi, gi in enumerate(gir):
        session.merge(
            usam.Gene(
                hgnc=gi.hgnc,
                maploc=gi.maploc,
                descr=gi.descr,
                summary=gi.summary,
                aliases=gi.aliases,
            ))
        logger.info("Added {gi.hgnc} ({gi.summary})".format(gi=gi))
    session.commit()


def load_txinfo(session, opts, cf):
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    self_aln_method = "transcript"
    update_period = 250

    mfdb = _get_mfdb(cf)

    n_rows = len(gzip.open(opts["FILE"]).readlines()) - 1
    tir = ufti.TxInfoReader(gzip.open(opts["FILE"]))
    logger.info("opened " + opts["FILE"])

    @lru_cache(maxsize=100)
    def _fetch_origin_by_name(name):
        try:
            ori = session.query(usam.Origin).filter(
                usam.Origin.name == name).one()
        except NoResultFound as e:
            logger.error("No origin for " + ti.origin)
            raise e
        return ori


    for i_ti, ti in enumerate(tir):
        if i_ti % update_period == 0 or i_ti + 1 == n_rows:
            logger.info("{i_ti}/{n_rows} {p:.1f}%: loading transcript {ac}".format(
                i_ti=i_ti+1, n_rows=n_rows, p=(i_ti + 1) / n_rows * 100, ac=ti.ac))

        if ti.exons_se_i == "":
            logger.warning(ti.ac + ": no exons?!; skipping.")
            continue

        ori = _fetch_origin_by_name(ti.origin)

        existing = session.query(usam.Transcript).filter(
            usam.Transcript.ac == ti.ac,
            usam.Transcript.origin_id == ori.origin_id)
        if existing.count() > 0:
            logger.warning("dropping existing {ti.origin}:{ti.ac}".format(ti=ti))
            existing.delete()

        if ti.cds_se_i:
            cds_start_i, cds_end_i = map(int, ti.cds_se_i.split(","))
            try:
                cds_seq = mfdb.fetch(ti.ac, cds_start_i, cds_end_i)
            except KeyError:
                #raise Exception("{ac}: not in sequence database; skipping".format(
                #    ac=ti.ac))
                logger.error("{ac}: not in sequence database; skipping".format(
                    ac=ti.ac))
                continue
            cds_md5 = seq_md5(cds_seq)
        else:
            cds_start_i = cds_end_i = None
            cds_md5 = None

        assert (cds_start_i is not None) ^ (cds_md5 is None), "failed: cds_start_i is None i.f.f. cds_md5_is None"

        u_tx = usam.Transcript(
            ac=ti.ac,
            origin=ori,
            hgnc=ti.hgnc,
            cds_start_i=cds_start_i,
            cds_end_i=cds_end_i,
            cds_md5=cds_md5,
        )
        session.add(u_tx)

        u_es = usam.ExonSet(
            transcript=u_tx,
            alt_ac=ti.ac,
            alt_strand=1,
            alt_aln_method=self_aln_method,
        )
        session.add(u_es)

        exons = [map(int, ex.split(",")) for ex in ti.exons_se_i.split(";")]
        for i_ex, ex in enumerate(exons):
            s, e = ex
            u_ex = usam.Exon(
                exon_set=u_es,
                start_i=s,
                end_i=e,
                ord=i_ex,
            )
            session.add(u_ex)

        session.commit()


def align_exons(session, opts, cf):
    # N.B. setup.py declares dependencies for using uta as a client.  The
    # imports below are loading depenencies only and are not in setup.py.

    update_period = 1000

    def _get_cursor(con):
        cur = con.cursor(cursor_factory=psycopg2.extras.NamedTupleCursor)
        cur.execute("set role {admin_role};".format(
            admin_role=cf.get("uta", "admin_role")))
        cur.execute("set search_path = " + usam.schema_name)
        return cur

    def align(s1, s2):
        score, cigar = utaaa.needleman_wunsch_gotoh_align(str(s1),
                                                          str(s2),
                                                          extended_cigar=True)
        tx_aseq, alt_aseq = utaaa.cigar_alignment(
            tx_seq, alt_seq, cigar, hide_match=False)
        return tx_aseq, alt_aseq, cigar.to_string()

    aln_sel_sql = """
    SELECT * FROM tx_alt_exon_pairs_v TAEP
    WHERE exon_aln_id is NULL
    ORDER BY hgnc
    """

    aln_ins_sql = """
    INSERT INTO exon_aln (tx_exon_id,alt_exon_id,cigar,added,tx_aseq,alt_aseq)
    VALUES (%s,%s,%s,%s,%s,%s)
    """

    con = session.bind.pool.connect()
    cur = _get_cursor(con)
    cur.execute(aln_sel_sql)
    n_rows = cur.rowcount

    if n_rows == 0:
        return

    mfdb = _get_mfdb(cf)

    def _fetch_seq(ac, s, e):
        logger.info("fetching sequence {ac}[{s}:{e}]".format(ac=ac,s=s,e=e))
        seq = mfdb.fetch(ac,s,e)
        assert seq is not None, "sequence {ac}[{s}:{e}] should never be None (coordinates bogus?)".format(ac=ac,s=s,e=e)
        return seq

    rows = cur.fetchall()
    ac_warning = set()
    tx_acs = set()
    aln_rate_s = None
    decay_rate = 0.25
    n0, t0 = 0, time.time()

    for i_r, r in enumerate(rows):
        if r.tx_ac in ac_warning or r.alt_ac in ac_warning:
            continue

        try:
            tx_seq = _fetch_seq(r.tx_ac, r.tx_start_i, r.tx_end_i)
        except KeyError:
            logger.warning(
                "{r.tx_ac}: Not in sequence sources; can't align".format(r=r))
            ac_warning.add(r.tx_ac)
            continue

        try:
            alt_seq = _fetch_seq(r.alt_ac, r.alt_start_i, r.alt_end_i)
        except KeyError:
            logger.warning(
                "{r.alt_ac}: Not in sequence sources; can't align".format(r=r))
            ac_warning.add(r.tx_ac)
            continue

        if r.alt_strand == MINUS_STRAND:
            alt_seq = reverse_complement(alt_seq)
        tx_seq = tx_seq.upper()
        alt_seq = alt_seq.upper()

        tx_aseq, alt_aseq, cigar_str = align(tx_seq, alt_seq)

        added = datetime.datetime.now()
        cur.execute(aln_ins_sql, [r.tx_exon_id, r.alt_exon_id, cigar_str, added, tx_aseq, alt_aseq])
        tx_acs.add(r.tx_ac)

        if i_r > 0 and (i_r % update_period == 0 or (i_r + 1) == n_rows):
            con.commit()
            n1, t1 = i_r, time.time()
            nd, td = n1 - n0, t1 - t0
            aln_rate = nd / td      # aln rate on this update period
            if aln_rate_s is None:  # aln_rate_s is EWMA smoothed average
                aln_rate_s = aln_rate
            else:
                aln_rate_s = decay_rate * aln_rate + (1.0 - decay_rate) * aln_rate_s
            etr = (n_rows - i_r - 1) / aln_rate_s        # etr in secs
            etr_s = str(datetime.timedelta(seconds=round(etr)))  # etr as H:M:S
            logger.info("{i_r}/{n_rows} {p_r:.1f}%; committed; speed={speed:.1f}/{speed_s:.1f} aln/sec (inst/emwa); etr={etr:.0f}s ({etr_s}); {n_tx} tx".format(
                i_r=i_r, n_rows=n_rows, p_r=i_r / n_rows * 100, speed=aln_rate, speed_s=aln_rate_s, etr=etr,
                etr_s=etr_s, n_tx=len(tx_acs)))
            tx_acs = set()
            n0, t0 = n1, t1

    cur.close()
    con.close()


def load_ncbi_geneinfo(session, opts, cf):
    """
    import data as downloaded (by you) from 
    ftp://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_info.gz
    """

    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    gip = uta.parsers.geneinfo.GeneInfoParser(gzip.open(opts["FILE"]))
    for gi in gip:
        if gi["tax_id"] != "9606" or gi["Symbol_from_nomenclature_authority"] == "-":
            continue
        g = usam.Gene(
            gene_id=gi["GeneID"],
            hgnc=gi["Symbol_from_nomenclature_authority"],
            maploc=gi["map_location"],
            descr=gi["Full_name_from_nomenclature_authority"],
            aliases=gi["Synonyms"],
            strand=gi[""],
        )
        session.add(g)
        logger.info("loaded gene {g.hgnc} ({g.descr})".format(g=g))
    session.commit()


def load_sequences(session, opts, cf):

    # TODO: Don't store sequences in UTA
    # load sequences up to max_len in size
    # 2e6 was chosen empirically based on sizes of NMs, NGs, NWs, NTs, NCs
    max_len = int(2e6)

    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    mfdb = _get_mfdb(cf)

    # fetch accessions for given sequences
    sql = """
    select S.seq_id,S.len,array_agg(SA.ac order by SA.ac~'^ENST',SA.ac) as acs
    from seq S
    join seq_anno SA on S.seq_id=SA.seq_id
    where S.len <= {max_len} and S.seq is NULL
    group by S.seq_id,len
    """.format(max_len=max_len)

    def _fetch_first(acs):
        # try all accessions in acs, return sequence of first that returns a
        # sequence
        for ac in row["acs"]:
            try:
                return mfdb.fetch(ac)
            except KeyError:
                pass
        return None

    for row in session.execute(sql):
        seq = _fetch_first(row["acs"])
        if seq is None:
            logger.warn("No sequence found for {acs}".format(acs=row["acs"]))
            continue
        seq = seq.upper()
        if row["len"] != len(seq):
            logger.error("Expected a sequence of length {len} for {md5} ({acs}); got sequence of length {len2}".format(
                len=row["len"], md5=row["seq_id"], acs=row["acs"], len2=len(seq)))
            continue
        assert row["len"] == len(seq), "expected a sequence of length {len} for {md5} ({acs}); got length {len2}".format(
            len=row["len"], md5=row["seq_id"], acs=row["acs"], len2=len(seq))
        session.execute(usam.Seq.__table__.update().values(
            seq=seq).where(usam.Seq.seq_id == row["seq_id"]))
        logger.info("loaded sequence of length {len} for {md5} ({acs})".format(
            len=len(seq), md5=row["seq_id"], acs=row["acs"]))
        session.commit()


def load_ncbi_seqgene(session, opts, cf):
    """
    import data as downloaded (by you) as from
    ftp.ncbi.nih.gov/genomes/MapView/Homo_sapiens/sequence/current/initial_release/seq_gene.md.gz
    """
    def _seqgene_recs_to_tx_info(ac, assy, recs):
        ti = {
            "ac": ac,
            "assy": assy,
            "strand": strand_pm_to_int(recs[0]["chr_orient"]),
            "gene_id": int(recs[0]["feature_id"].replace("GeneID:", "")) if "GeneID" in recs[0]["feature_id"] else None,
        }
        segs = [(r["feature_type"], int(r["chr_start"]) - 1, int(r["chr_stop"]))
                for r in recs]
        cds_seg_idxs = [i for i in range(len(segs)) if segs[i][0] == "CDS"]
        # merge UTR-CDS and CDS-UTR exons if end of first == start of second
        # prefer this over general adjacent exon merge in case of alignment artifacts
        # last exon
        ei = cds_seg_idxs[-1]
        ti["cds_end_i"] = segs[ei][2]
        if ei < len(segs) - 1:
            if segs[ei][2] == segs[ei + 1][1]:
                segs[ei:ei + 2] = [("M", segs[ei][1], segs[ei + 1][2])]
        # first exon
        ei = cds_seg_idxs[0]
        ti["cds_start_i"] = segs[ei][1]
        if ei > 0:
            if segs[ei - 1][2] == segs[ei][1]:
                segs[ei - 1:ei + 1] = [("M", segs[ei - 1][1], segs[ei][2])]
        ti["exon_se_i"] = [s[1:3] for s in segs]
        return ti


    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    o_refseq = session.query(usam.Origin).filter(
        usam.Origin.name == "NCBI RefSeq").one()

    sg_filter = lambda r: (r["transcript"].startswith("NM_")
                           and r["group_label"] == "GRCh37.p10-Primary Assembly"
                           and r["feature_type"] in ["CDS", "UTR"])
    sgparser = uta.parsers.seqgene.SeqGeneParser(gzip.open(opts["FILE"]),
                                                 filter=sg_filter)
    slurp = sorted(list(sgparser),
                   key=lambda r: (r["transcript"], r["group_label"], r["chr_start"], r["chr_stop"]))
    for k, i in itertools.groupby(slurp, key=lambda r: (r["transcript"], r["group_label"])):
        ac, assy = k
        ti = _seqgene_recs_to_tx_info(ac, assy, list(i))

        resp = session.query(usam.Transcript).filter(usam.Transcript.ac == ac)
        if resp.count() == 0:
            t = usam.Transcript(ac=ac, origin=o_refseq, gene_id=ti["gene_id"])
            session.add(t)
        else:
            t = resp.one()

        es = usam.ExonSet(
            transcript_id=t.transcript_id,
            ref_nseq_id=99,
            strand=ti["strand"],
            cds_start_i=ti["cds_start_i"],
            cds_end_i=ti["cds_end_i"],
        )


def grant_permissions(session, opts, cf):
    schema = usam.schema_name

    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    cmds = [
        # alter db doesn't belong here, and probably better to avoid the implicit behevior this encourages
        # "alter database {db} set search_path = {schema}".format(db=cf.get("uta", "database"),schema=schema),
        "grant usage on schema " + schema + " to PUBLIC",
    ]

    sql = "select concat(schemaname,'.',tablename) as fqrn from pg_tables where schemaname='{schema}'".format(
        schema=schema)
    rows = list(session.execute(sql))
    cmds += ["grant select on {fqrn} to PUBLIC".format(
        fqrn=row["fqrn"]) for row in rows]
    cmds += ["alter table {fqrn} owner to uta_admin".format(
        fqrn=row["fqrn"]) for row in rows]

    sql = "select concat(schemaname,'.',viewname) as fqrn from pg_views where schemaname='{schema}'".format(
        schema=schema)
    rows = list(session.execute(sql))
    cmds += ["grant select on {fqrn} to PUBLIC".format(
        fqrn=row["fqrn"]) for row in rows]
    cmds += ["alter view {fqrn} owner to uta_admin".format(
        fqrn=row["fqrn"]) for row in rows]

    sql = "select concat(schemaname,'.',matviewname) as fqrn from pg_matviews where schemaname='{schema}'".format(
        schema=schema)
    rows = list(session.execute(sql))
    cmds += ["grant select on {fqrn} to PUBLIC".format(
        fqrn=row["fqrn"]) for row in rows]
    cmds += ["alter materialized view {fqrn} owner to uta_admin".format(
        fqrn=row["fqrn"]) for row in rows]

    for cmd in sorted(cmds):
        logger.info(cmd)
        session.execute(cmd)
    session.commit()


def refresh_matviews(session, opts, cf):
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)

    # matviews must be updated in dependency order. Unfortunately,
    # it's difficult to determine this programmatically. The "right"
    # solution is a recursive CTE, but I was unable to find or write
    # one readily. This function should be consdered a placeholder for
    # the right way to update, which doesn't exist yet.

    # TODO: Determine mv refresh order programmatically.
    # sql = "select concat(schemaname,".",matviewname) as fqrn from pg_matviews where schemaname="{schema}"".format(
    #     schema=schema)
    # rows = list(session.execute(sql))
    # cmds = [ "refresh materialized view {fqrn}".format(fqrn=row["fqrn"]) for row in rows ]

    cmds = [
        # N.B. Order matters!
        "refresh materialized view exon_set_exons_fp_mv",
        "refresh materialized view tx_exon_set_summary_mv",
        "refresh materialized view tx_def_summary_mv",
        # "refresh materialized view tx_aln_cigar_mv",
        # "refresh materialized view tx_aln_summary_mv",
    ]

    for cmd in cmds:
        logger.info(cmd)
        session.execute(cmd)
    session.commit()


def analyze(session, opts, cf):
    session.execute("set role {admin_role};".format(
        admin_role=cf.get("uta", "admin_role")))
    session.execute("set search_path = " + usam.schema_name)
    cmds = [
        "analyze verbose"
    ]
    for cmd in cmds:
        logger.info(cmd)
        session.execute(cmd)
    session.commit()


def _get_mfdb(cf):
    fa_dirs = cf.get("sequences", "fasta_directories").strip().splitlines()
    mfdb = MultiFastaDB(fa_dirs, use_meta_index=True)
    logger.info("Opened sequence directories: " + ",".join(fa_dirs))
    return mfdb



# <LICENSE>
# Copyright 2014 UTA Contributors (https://bitbucket.org/biocommons/uta)
##
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
##
# http://www.apache.org/licenses/LICENSE-2.0
##
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# </LICENSE>
