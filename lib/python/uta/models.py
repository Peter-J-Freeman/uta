"""Schema for Universal Transcript Archive
"""

import datetime

import sqlalchemy as sa
import sqlalchemy.ext.declarative as saed

schema_version = '0'
schema_name = 'uta'+schema_version

Base = saed.declarative_base()


class Meta(Base):
    __tablename__ = 'meta'
    __table_args__ = {'schema' : schema_name}
    key = sa.Column(sa.String, primary_key=True, nullable=False, index=True)
    value = sa.Column(sa.String, nullable=False)


class Gene(Base):
    __tablename__ = 'gene'
    __table_args__ = (
        sa.CheckConstraint('strand = -1 or strand = 1', 'strand_is_plus_or_minus_1'),
        {'schema' : schema_name}
        )
    gene_id = sa.Column(sa.Integer, sa.Sequence('gene_id_seq'), primary_key=True, index=True)
    added = sa.Column(sa.DateTime, nullable=False, default=datetime.datetime.now() )
    gene = sa.Column(sa.String, index=True, unique=True, nullable=False)
    maploc = sa.Column(sa.String)
    strand = sa.Column(sa.SMALLINT)
    descr = sa.Column(sa.String)
    summary = sa.Column(sa.String)

class Exon(Base):
    __tablename__ = 'exon'
    __table_args__ = {'schema' : schema_name}
    exon_id = sa.Column(sa.Integer, sa.Sequence('exon_id_seq'), primary_key=True, index=True)
    transcript_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.transcript.transcript_id'), nullable=False)
    start_i = sa.Column(sa.Integer, nullable=False)
    end_i = sa.Column(sa.Integer, nullable=False)
    name = sa.Column(sa.String)
    seq = sa.Column(sa.String)
    
class ExonAlignment(Base):
    __tablename__ = 'exon_alignment'
    __table_args__ = (
        sa.CheckConstraint('exon_id1 < exon_id2'),
        {'schema' : schema_name}
        )
    exon_alignment_id = sa.Column(sa.Integer, sa.Sequence('exon_alignment_id_seq'), primary_key=True, index=True)
    added = sa.Column(sa.DateTime, default=datetime.datetime.now(), nullable=False)
    exon_id1 = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.exon.exon_id'), nullable=False)
    exon_id2 = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.exon.exon_id'), nullable=False)
    cigar = sa.Column(sa.String, nullable=False)
    alignment = sa.Column(sa.String, nullable=True)

class NSeq(Base):
    __tablename__ = 'nseq'
    __table_args__ = {'schema' : schema_name}
    nseq_id = sa.Column(sa.Integer, sa.Sequence('nseq_id_seq'), primary_key=True, index=True)
    origin_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.origin.origin_id'), nullable=False)
    ac = sa.Column(sa.String, nullable=False)
    added = sa.Column(sa.DateTime, nullable=False, default=datetime.datetime.now() )
    md5 = sa.Column(sa.String, nullable=True)
    seq = sa.Column(sa.String, nullable=True)

class Origin(Base):
    __tablename__ = 'origin'
    __table_args__ = {'schema' : schema_name}
    origin_id = sa.Column(sa.Integer, sa.Sequence('origin_id_seq'), primary_key=True, index=True)
    name = sa.Column(sa.String, nullable=False)
    added = sa.Column(sa.DateTime, nullable=False, default=datetime.datetime.now() )
    url = sa.Column(sa.String, nullable=True)
    url_fmt = sa.Column(sa.String, nullable=True)

class OriginTranscriptAlias(Base):
    __tablename__ = 'origin_transcript_alias'
    __table_args__ = {'schema' : schema_name}
    transcript_id = sa.Column(sa.Integer, sa.Sequence('transcript_id_seq'), primary_key=True, index=True)
    origin_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.origin.origin_id'), nullable=False)
    nseq_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.nseq.nseq_id'), nullable=False)
    added = sa.Column(sa.DateTime, default=datetime.datetime.now(), nullable=False)
    gene = sa.Column(sa.String)
    gene_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.gene.gene_id'))

class Transcript(Base):
    __tablename__ = 'transcript'
    __table_args__ = (
        sa.CheckConstraint('strand = -1 or strand = 1', 'strand_is_plus_or_minus_1'),
        {'schema' : schema_name}
        )
    transcript_id = sa.Column(sa.Integer, sa.Sequence('transcript_id_seq'), primary_key=True, index=True)
    nseq_id = sa.Column(sa.Integer, sa.ForeignKey(schema_name+'.nseq.nseq_id'), nullable=False)
    added = sa.Column(sa.DateTime, default=datetime.datetime.now(), nullable=False)
    strand = sa.Column(sa.SMALLINT, nullable=False)
    cds_start_i = sa.Column(sa.Integer, nullable=False)
    cds_end_i = sa.Column(sa.Integer, nullable=False)