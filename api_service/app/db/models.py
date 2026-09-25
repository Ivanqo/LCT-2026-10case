from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from .session import Base


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    slug = Column(String(255), unique=True, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    users = relationship("User", back_populates="organization", cascade="all, delete-orphan")
    projects = relationship("Project", back_populates="organization", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    login = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(512), nullable=False)
    api_key = Column(String(128), unique=True, nullable=False, index=True)

    is_admin = Column(Boolean, nullable=False, server_default="0")
    role = Column(String(32), nullable=False, server_default="INSPECTOR", index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    organization = relationship("Organization", back_populates="users")
    qa_sessions = relationship("QASession", back_populates="user")
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", back_populates="sessions")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)

    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_projects_org_name"),
    )

    organization = relationship("Organization", back_populates="projects")
    qa_sessions = relationship("QASession", back_populates="project", cascade="all, delete-orphan")
    document_versions = relationship("DocumentVersion", back_populates="project", cascade="all, delete-orphan")
    canonical_entities = relationship("CanonicalEntity", back_populates="project", cascade="all, delete-orphan")
    construction_objects = relationship("ConstructionObject", back_populates="project", cascade="all, delete-orphan")
    matrix_versions = relationship("MatrixVersion", back_populates="project", cascade="all, delete-orphan")
    inspection_processes = relationship("InspectionProcess", back_populates="project", cascade="all, delete-orphan")
    protocols = relationship("Protocol", back_populates="project", cascade="all, delete-orphan")


class QASession(Base):
    __tablename__ = "qa_sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    user_label = Column(String(255), nullable=True)
    references = Column(Text, nullable=True)

    project = relationship("Project", back_populates="qa_sessions")
    user = relationship("User", back_populates="qa_sessions")


class UploadJob(Base):
    __tablename__ = "upload_jobs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    filename = Column(String(512), nullable=False)
    file_type = Column(String(32), nullable=False)
    temp_path = Column(String(2048), nullable=True)
    file_size = Column(Integer, nullable=True)
    process_id = Column(String(64), nullable=True, index=True)
    source_type = Column(String(32), nullable=False, server_default="rag")
    status = Column(String(64), nullable=False, server_default="queued", index=True)
    stage = Column(String(255), nullable=True)
    progress = Column(Integer, nullable=False, server_default="0")
    detail = Column(Text, nullable=True)
    document_id = Column(Integer, nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    finished_at = Column(DateTime, nullable=True)

class ProjectDocument(Base):
    __tablename__ = "project_documents"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    filename = Column(String(512), nullable=False)
    file_type = Column(String(32), nullable=False)
    storage_path = Column(String(2048), nullable=False)
    meta_path = Column(String(2048), nullable=True)
    pages_count = Column(Integer, nullable=False, server_default="0")
    first_page_text = Column(Text, nullable=True)
    status = Column(String(64), nullable=False, server_default="ready", index=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class UserCredentialRecord(Base):
    __tablename__ = "user_credential_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)

    login = Column(String(255), nullable=False, index=True)
    email = Column(String(255), nullable=True)
    password_plain = Column(String(255), nullable=True)
    api_key = Column(String(128), nullable=False)
    is_admin = Column(Boolean, nullable=False, server_default="0")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(32), nullable=False, server_default="rag", index=True)
    source_document_id = Column(Integer, nullable=True, index=True)

    object_id = Column(String(64), nullable=True, index=True)
    dataset_file_id = Column(String(128), nullable=True, index=True)
    dataset_split = Column(String(64), nullable=True, index=True)
    dataset_stage = Column(String(64), nullable=True, index=True)
    dataset_section = Column(String(128), nullable=True, index=True)
    dataset_metadata = Column(JSON, nullable=True)
    filename = Column(String(512), nullable=False)
    doc_stage = Column(String(64), nullable=True, index=True)
    document_stage = Column(String(64), nullable=False, server_default="unknown", index=True)
    discipline = Column(String(64), nullable=True, index=True)
    document_code = Column(String(255), nullable=True, index=True)
    version = Column(String(64), nullable=True)
    revision = Column(String(64), nullable=True)
    approval_status = Column(String(64), nullable=False, server_default="UNKNOWN", index=True)
    approval_date = Column(DateTime, nullable=True)
    content_hash = Column(String(128), nullable=True, index=True)
    file_hash = Column(String(128), nullable=True, index=True)
    file_path = Column(String(2048), nullable=True)
    predecessor_id = Column(Integer, ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True, index=True)
    successor_id = Column(Integer, ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True, index=True)
    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="document_versions")
    fragments = relationship("SourceFragment", back_populates="document_version", cascade="all, delete-orphan")
    observations = relationship("EntityObservation", back_populates="document_version", cascade="all, delete-orphan")
    predecessor = relationship("DocumentVersion", remote_side=[id], foreign_keys=[predecessor_id], post_update=True)
    successor = relationship("DocumentVersion", remote_side=[id], foreign_keys=[successor_id], post_update=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "organization_id",
            "source_type",
            "source_document_id",
            "filename",
            name="uq_document_versions_source",
        ),
    )


class SourceFragment(Base):
    __tablename__ = "source_fragments"

    id = Column(Integer, primary_key=True, index=True)
    document_version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    page = Column(Integer, nullable=True, index=True)
    bbox = Column(JSON, nullable=True)
    bbox_pdf = Column(JSON, nullable=True)
    page_width = Column(Float, nullable=True)
    page_height = Column(Float, nullable=True)
    text = Column(Text, nullable=True)
    fragment_type = Column(String(64), nullable=False, server_default="text", index=True)
    source_system = Column(String(64), nullable=False, server_default="manual", index=True)
    external_id = Column(String(128), nullable=True, index=True)
    metadata_json = Column(JSON, nullable=True)
    extractor = Column(String(128), nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    document_version = relationship("DocumentVersion", back_populates="fragments")
    entity_observations = relationship("EntityObservation", back_populates="source_fragment")
    attribute_observations = relationship("AttributeObservation", back_populates="source_fragment")

    __table_args__ = (
        UniqueConstraint("document_version_id", "source_system", "external_id", name="uq_source_fragment_external_ref"),
    )


class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"

    id = Column(String(64), primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String(64), nullable=False, index=True)
    canonical_name = Column(String(512), nullable=False)
    status = Column(String(64), nullable=False, server_default="active", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="canonical_entities")
    aliases = relationship("EntityAlias", back_populates="canonical_entity", cascade="all, delete-orphan")
    observations = relationship("EntityObservation", back_populates="canonical_entity")
    outgoing_relations = relationship(
        "EntityRelation",
        foreign_keys="EntityRelation.from_entity_id",
        back_populates="from_entity",
        cascade="all, delete-orphan",
    )
    incoming_relations = relationship(
        "EntityRelation",
        foreign_keys="EntityRelation.to_entity_id",
        back_populates="to_entity",
        cascade="all, delete-orphan",
    )
    change_events = relationship("ChangeEvent", back_populates="canonical_entity", cascade="all, delete-orphan")


class EntityAlias(Base):
    __tablename__ = "entity_aliases"

    id = Column(Integer, primary_key=True, index=True)
    canonical_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    alias = Column(String(512), nullable=False)
    normalized_alias = Column(String(512), nullable=False, index=True)
    alias_type = Column(String(64), nullable=False, server_default="name", index=True)
    confidence = Column(Float, nullable=True)
    source = Column(String(128), nullable=True)
    status = Column(String(64), nullable=False, server_default="active", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    canonical_entity = relationship("CanonicalEntity", back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("canonical_entity_id", "normalized_alias", "alias_type", name="uq_entity_alias_entity_value_type"),
    )


class EntityObservation(Base):
    __tablename__ = "entity_observations"

    id = Column(Integer, primary_key=True, index=True)
    canonical_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="SET NULL"), nullable=True, index=True)
    document_version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    source_fragment_id = Column(Integer, ForeignKey("source_fragments.id", ondelete="SET NULL"), nullable=True, index=True)

    raw_name = Column(String(512), nullable=True, index=True)
    raw_mark = Column(String(255), nullable=True, index=True)
    normalized_name = Column(String(512), nullable=True, index=True)
    stage = Column(String(64), nullable=True, index=True)
    location = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)
    extractor = Column(String(128), nullable=True)
    resolution_explanation = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    canonical_entity = relationship("CanonicalEntity", back_populates="observations")
    document_version = relationship("DocumentVersion", back_populates="observations")
    source_fragment = relationship("SourceFragment", back_populates="entity_observations")
    attributes = relationship("AttributeObservation", back_populates="entity_observation", cascade="all, delete-orphan")


class AttributeObservation(Base):
    __tablename__ = "attribute_observations"

    id = Column(Integer, primary_key=True, index=True)
    entity_observation_id = Column(Integer, ForeignKey("entity_observations.id", ondelete="CASCADE"), nullable=False, index=True)
    source_fragment_id = Column(Integer, ForeignKey("source_fragments.id", ondelete="SET NULL"), nullable=True, index=True)

    attribute_name = Column(String(128), nullable=False, index=True)
    raw_value = Column(Text, nullable=True)
    normalized_value = Column(Text, nullable=True)
    normalized_numeric = Column(Float, nullable=True)
    unit = Column(String(64), nullable=True)
    confidence = Column(Float, nullable=True)
    extractor = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    entity_observation = relationship("EntityObservation", back_populates="attributes")
    source_fragment = relationship("SourceFragment", back_populates="attribute_observations")


class EntityRelation(Base):
    __tablename__ = "entity_relations"

    id = Column(Integer, primary_key=True, index=True)
    from_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    to_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type = Column(String(64), nullable=False, index=True)
    confidence = Column(Float, nullable=True)
    source_fragment_id = Column(Integer, ForeignKey("source_fragments.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    from_entity = relationship("CanonicalEntity", foreign_keys=[from_entity_id], back_populates="outgoing_relations")
    to_entity = relationship("CanonicalEntity", foreign_keys=[to_entity_id], back_populates="incoming_relations")
    source_fragment = relationship("SourceFragment")

    __table_args__ = (
        UniqueConstraint("from_entity_id", "to_entity_id", "relation_type", name="uq_entity_relation_pair_type"),
    )


class ChangeEvent(Base):
    __tablename__ = "change_events"

    id = Column(Integer, primary_key=True, index=True)
    canonical_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    attribute = Column(String(128), nullable=False, index=True)
    previous_observation_id = Column(Integer, ForeignKey("attribute_observations.id", ondelete="SET NULL"), nullable=True)
    next_observation_id = Column(Integer, ForeignKey("attribute_observations.id", ondelete="SET NULL"), nullable=True)
    change_type = Column(String(64), nullable=False, index=True)
    delta = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    canonical_entity = relationship("CanonicalEntity", back_populates="change_events")
    previous_observation = relationship("AttributeObservation", foreign_keys=[previous_observation_id])
    next_observation = relationship("AttributeObservation", foreign_keys=[next_observation_id])
    issue = relationship("ChangeIssue", back_populates="change_event", uselist=False, cascade="all, delete-orphan")


class ChangeIssue(Base):
    __tablename__ = "change_issues"

    id = Column(Integer, primary_key=True, index=True)
    change_event_id = Column(Integer, ForeignKey("change_events.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    risk_score = Column(Float, nullable=False, server_default="0")
    severity = Column(String(64), nullable=False, server_default="NEEDS_REVIEW", index=True)
    explanation = Column(Text, nullable=True)
    status = Column(String(64), nullable=False, server_default="New", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    change_event = relationship("ChangeEvent", back_populates="issue")
    decisions = relationship("ReviewDecision", back_populates="issue", cascade="all, delete-orphan")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("change_issues.id", ondelete="CASCADE"), nullable=False, index=True)
    decision = Column(String(64), nullable=False, index=True)
    comment = Column(Text, nullable=True)
    reviewer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    issue = relationship("ChangeIssue", back_populates="decisions")
    reviewer = relationship("User")


class ConstructionObject(Base):
    __tablename__ = "construction_objects"

    id = Column(String(64), primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(512), nullable=False)
    address = Column(Text, nullable=True)
    customer = Column(String(512), nullable=True)
    contractor = Column(String(512), nullable=True)
    permit_number = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="construction_objects")


class MatrixVersion(Base):
    __tablename__ = "matrix_versions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(String(64), nullable=False, index=True)
    status = Column(String(64), nullable=False, server_default="DRAFT", index=True)
    source_filename = Column(String(512), nullable=True)
    source_hash = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="matrix_versions")
    params = relationship("Param", back_populates="matrix_version", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("organization_id", "project_id", "version", name="uq_matrix_versions_scope_version"),
    )


class Param(Base):
    __tablename__ = "params"

    id = Column(Integer, primary_key=True, index=True)
    matrix_version_id = Column(Integer, ForeignKey("matrix_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    code = Column(String(64), nullable=False, index=True)
    matrix_code = Column(String(64), nullable=True, index=True)
    scoring_code = Column(String(64), nullable=True, index=True)
    aliases_json = Column(JSON, nullable=True)
    section = Column(String(255), nullable=True, index=True)
    parameter_name = Column(String(512), nullable=False)
    unit = Column(String(64), nullable=True)

    source_pd = Column(Boolean, nullable=False, server_default="1")
    source_rd = Column(Boolean, nullable=False, server_default="1")
    source_id = Column(Boolean, nullable=False, server_default="1")

    trigger_logic = Column(Text, nullable=True)
    review_priority = Column(String(16), nullable=False, server_default="MEDIUM", index=True)

    sp_reference = Column(Text, nullable=True)
    gost_reference = Column(Text, nullable=True)
    fz_reference = Column(Text, nullable=True)
    other_normative = Column(Text, nullable=True)

    data_type = Column(String(64), nullable=False, server_default="string")
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)
    regex_pattern = Column(Text, nullable=True)

    is_active = Column(Boolean, nullable=False, server_default="1", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    matrix_version = relationship("MatrixVersion", back_populates="params")

    __table_args__ = (
        UniqueConstraint("matrix_version_id", "code", name="uq_params_matrix_code"),
    )


class InspectionProcess(Base):
    __tablename__ = "inspection_processes"

    id = Column(String(64), primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)

    status = Column(String(32), nullable=False, server_default="PENDING", index=True)
    upload_scenario = Column(String(64), nullable=False, server_default="SINGLE_ONLY", index=True)
    completeness_status = Column(JSON, nullable=True)
    affected_param_codes = Column(JSON, nullable=True)

    matrix_version = Column(String(64), nullable=False, server_default="demo-fixture-v3")
    dataset_version = Column(String(64), nullable=False, server_default="case10-local-v3")
    model_version = Column(String(64), nullable=False, server_default="rule-extractors-v1")
    input_manifest_hash = Column(String(128), nullable=True, index=True)

    retry_count = Column(Integer, nullable=False, server_default="0")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    finalized_at = Column(DateTime, nullable=True)
    finalized_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    project = relationship("Project", back_populates="inspection_processes")
    evidence_groups = relationship("EvidenceGroup", back_populates="process", cascade="all, delete-orphan")
    protocols = relationship("Protocol", back_populates="process", cascade="all, delete-orphan")
    jobs = relationship("Case10ProcessJob", back_populates="process", cascade="all, delete-orphan")


class Case10ProcessJob(Base):
    __tablename__ = "case10_process_jobs"

    id = Column(String(64), primary_key=True, index=True)
    process_id = Column(String(64), ForeignKey("inspection_processes.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    status = Column(String(32), nullable=False, server_default="QUEUED", index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="3")
    payload_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    retry_reason = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    process = relationship("InspectionProcess", back_populates="jobs")
    user = relationship("User")


class EvidenceGroup(Base):
    __tablename__ = "evidence_groups"

    id = Column(Integer, primary_key=True, index=True)
    process_id = Column(String(64), ForeignKey("inspection_processes.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)
    canonical_entity_id = Column(String(64), ForeignKey("canonical_entities.id", ondelete="SET NULL"), nullable=True, index=True)
    param_id = Column(Integer, ForeignKey("params.id", ondelete="SET NULL"), nullable=True, index=True)

    matrix_version = Column(String(64), nullable=False, server_default="demo-fixture-v3")
    model_version = Column(String(64), nullable=False, server_default="rule-extractors-v1")
    dataset_version = Column(String(64), nullable=False, server_default="case10-local-v3")

    comparison_scenario = Column(String(64), nullable=False, server_default="SINGLE_ONLY", index=True)
    completeness_status = Column(String(64), nullable=False, server_default="PARTIALLY_LOADED", index=True)
    comparability_status = Column(String(64), nullable=False, server_default="NOT_COMPARABLE", index=True)
    finding_status = Column(String(64), nullable=False, server_default="MISSING_EVIDENCE", index=True)

    expected_value = Column(Text, nullable=True)
    actual_value = Column(Text, nullable=True)
    delta = Column(JSON, nullable=True)
    review_priority = Column(String(16), nullable=False, server_default="MEDIUM", index=True)
    confidence = Column(Float, nullable=True)

    # Stable identity within (process_id, param_id) across incremental
    # recomputes -- e.g. a rule-pack location string, or the canonical entity
    # id -- used to match a freshly recomputed finding against the row that
    # may already carry an inspector decision, instead of a blind
    # delete-then-recreate that would silently wipe decision history.
    group_key = Column(String(255), nullable=True, index=True)
    evidence_basis_hash = Column(String(128), nullable=True, index=True)
    needs_reverification = Column(Boolean, nullable=False, server_default="0", index=True)
    basis_changed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    process = relationship("InspectionProcess", back_populates="evidence_groups")
    param = relationship("Param")
    canonical_entity = relationship("CanonicalEntity")
    fragments = relationship("EvidenceFragment", back_populates="evidence_group", cascade="all, delete-orphan")
    decisions = relationship("EvidenceDecision", back_populates="evidence_group", cascade="all, delete-orphan")
    suspicion = relationship("Suspicion", back_populates="evidence_group", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        UniqueConstraint("process_id", "param_id", "canonical_entity_id", name="uq_evidence_group_process_param_entity"),
    )


class EvidenceFragment(Base):
    __tablename__ = "evidence_fragments"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    document_version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    source_fragment_id = Column(Integer, ForeignKey("source_fragments.id", ondelete="SET NULL"), nullable=True, index=True)

    file_sha256 = Column(String(128), nullable=True, index=True)
    dataset_file_id = Column(String(128), nullable=True, index=True)
    stage = Column(String(64), nullable=True, index=True)
    discipline = Column(String(64), nullable=True, index=True)
    document_code = Column(String(255), nullable=True, index=True)
    revision = Column(String(64), nullable=True)
    approval_status = Column(String(64), nullable=True, index=True)
    page = Column(Integer, nullable=True, index=True)
    bbox = Column(JSON, nullable=True)
    bbox_pdf = Column(JSON, nullable=True)
    page_width = Column(Float, nullable=True)
    page_height = Column(Float, nullable=True)
    polygon = Column(JSON, nullable=True)
    extracted_value = Column(Text, nullable=True)
    role = Column(String(32), nullable=False, server_default="context", index=True)
    context = Column(Text, nullable=True)
    extractor = Column(String(128), nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    evidence_group = relationship("EvidenceGroup", back_populates="fragments")
    document_version = relationship("DocumentVersion")
    source_fragment = relationship("SourceFragment")


class EvidenceDecision(Base):
    __tablename__ = "evidence_decisions"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    decision = Column(String(64), nullable=False, index=True)
    reason_code = Column(String(64), nullable=True, index=True)
    comment = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    evidence_group = relationship("EvidenceGroup", back_populates="decisions")
    user = relationship("User")


class Protocol(Base):
    __tablename__ = "protocols"

    id = Column(Integer, primary_key=True, index=True)
    process_id = Column(String(64), ForeignKey("inspection_processes.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)
    version = Column(Integer, nullable=False, index=True)

    matrix_version = Column(String(64), nullable=False)
    dataset_version = Column(String(64), nullable=False)
    model_version = Column(String(64), nullable=False)
    input_manifest_hash = Column(String(128), nullable=True, index=True)
    status = Column(String(64), nullable=False, server_default="DRAFT", index=True)
    payload_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    finalized_at = Column(DateTime, nullable=True)
    finalized_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    unfinalized_at = Column(DateTime, nullable=True)
    unfinalized_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    unfinalize_reason = Column(Text, nullable=True)

    project = relationship("Project", back_populates="protocols")
    process = relationship("InspectionProcess", back_populates="protocols")

    __table_args__ = (
        UniqueConstraint("process_id", "version", name="uq_protocol_process_version"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(128), nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)
    project_id = Column(Integer, nullable=True, index=True)
    process_id = Column(String(64), nullable=True, index=True)
    details = Column(JSON, nullable=True)
    ip_address = Column(String(128), nullable=True)
    user_agent = Column(String(512), nullable=True)
    timestamp = Column(DateTime, nullable=False, server_default=func.now(), index=True)

    user = relationship("User")


class GoldDraftItem(Base):
    __tablename__ = "gold_draft_items"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    decision_id = Column(Integer, ForeignKey("evidence_decisions.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)
    label = Column(String(64), nullable=False, index=True)
    payload_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    evidence_group = relationship("EvidenceGroup")
    decision = relationship("EvidenceDecision")


class GoldCheckFixture(Base):
    __tablename__ = "gold_check_fixtures"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    source_dataset = Column(String(64), nullable=False, index=True)
    check_id = Column(String(128), nullable=False, index=True)
    finding_group_id = Column(String(128), nullable=True, index=True)
    object_id = Column(String(64), nullable=False, index=True)
    split = Column(String(64), nullable=True, index=True)
    visibility = Column(String(64), nullable=True, index=True)
    matrix_scope = Column(String(64), nullable=True, index=True)
    parameter_id = Column(Integer, nullable=True)
    parameter_code = Column(String(64), nullable=False, index=True)
    location_type = Column(String(64), nullable=True, index=True)
    location = Column(String(255), nullable=True, index=True)
    violation_label = Column(String(64), nullable=False, index=True)
    protocol_status = Column(String(64), nullable=True, index=True)
    criticality = Column(String(255), nullable=True)
    inspector_status = Column(String(64), nullable=True, index=True)
    gold_status = Column(String(64), nullable=True, index=True)
    score_eligible = Column(Boolean, nullable=False, server_default="0", index=True)
    training_allowed = Column(Boolean, nullable=False, server_default="0", index=True)
    evaluation_allowed = Column(Boolean, nullable=False, server_default="0", index=True)
    leakage_guard = Column(JSON, nullable=True)
    evidence_json = Column(JSON, nullable=True)
    payload_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "organization_id", "source_dataset", "check_id", name="uq_gold_fixture_source_check"),
    )


class MLRetrainingLog(Base):
    __tablename__ = "ml_retraining_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_version = Column(String(128), nullable=False, index=True)
    matrix_version = Column(String(64), nullable=True, index=True)
    model_version = Column(String(128), nullable=True, index=True)
    candidate_model_version = Column(String(128), nullable=True, index=True)
    split_hashes = Column(JSON, nullable=True)
    metrics_json = Column(JSON, nullable=True)
    per_category_metrics = Column(JSON, nullable=True)
    approval_status = Column(String(64), nullable=False, server_default="BLOCKED", index=True)
    gate_status = Column(String(64), nullable=False, server_default="BLOCKED", index=True)
    gate_reasons = Column(JSON, nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    baseline_metrics_json = Column(JSON, nullable=True)
    release_payload_json = Column(JSON, nullable=True)
    rollback_to_model_version = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    approved_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    approved_by = relationship("User")


class ModelVersionRegistry(Base):
    __tablename__ = "model_version_registry"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    model_version = Column(String(128), nullable=False, index=True)
    dataset_version = Column(String(128), nullable=False, index=True)
    matrix_version = Column(String(64), nullable=True, index=True)
    artifact_hash = Column(String(128), nullable=True, index=True)
    metrics_json = Column(JSON, nullable=True)
    approval_status = Column(String(64), nullable=False, server_default="DRAFT", index=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    deployed_at = Column(DateTime, nullable=True)
    rollback_to_model_version = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    project = relationship("Project")
    approved_by = relationship("User")

    __table_args__ = (
        UniqueConstraint("project_id", "organization_id", "model_version", name="uq_model_version_registry_project_version"),
    )


class ProcessingCache(Base):
    __tablename__ = "processing_cache"

    id = Column(Integer, primary_key=True, index=True)
    cache_key = Column(String(255), nullable=False, unique=True, index=True)
    input_hash = Column(String(128), nullable=False, index=True)
    result_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# -- ТЗ раздел 10 "Таблицы базы данных (сводка)": tables 6-10 and 13 of that
# summary that had no DB-level counterpart before (Params/Checks/Objects/Files/
# Protocols/ML_Retraining_Log/Audit_Log already exist above under this
# codebase's own names -- Param/EvidenceGroup/ConstructionObject/DocumentVersion
# /Protocol/MLRetrainingLog/AuditLog). Column names below follow this codebase's
# existing vocabulary (e.g. `evidence_group_id` instead of the ТЗ's
# `violation_id`) rather than the ТЗ's literal names, consistent with how every
# other table in this file already diverges from its ТЗ counterpart.


class RejectionLog(Base):
    """ТЗ-10 `Rejection_Log`: written whenever an inspector's decision resolves
    a finding to NEGATIVE_VERIFIED (see `record_inspector_decision` in
    v3_pipeline.py). `ai_verdict`/`ai_verdict_comment`/`suggested_fix` stay
    NULL until Module 9.4 ("Обратная связь ИИ") is implemented -- there is no
    automated AGREE/DISAGREE reasoning in this codebase yet, so this table
    honestly leaves those columns unset rather than fabricating a verdict."""

    __tablename__ = "rejection_logs"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    evidence_decision_id = Column(Integer, ForeignKey("evidence_decisions.id", ondelete="SET NULL"), nullable=True, index=True)
    process_id = Column(String(64), nullable=True, index=True)
    project_id = Column(Integer, nullable=True, index=True)
    organization_id = Column(Integer, nullable=True, index=True)

    rejection_reason = Column(Text, nullable=False)
    ai_verdict = Column(String(32), nullable=True, index=True)
    ai_verdict_comment = Column(Text, nullable=True)
    suggested_fix = Column(Text, nullable=True)
    retraining_status = Column(String(32), nullable=False, server_default="PENDING", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    evidence_group = relationship("EvidenceGroup")
    evidence_decision = relationship("EvidenceDecision")


class DisputeLog(Base):
    """ТЗ-10 `Dispute_Log`: a disputed finding, opened via
    `POST /case10/evidence-groups/{id}/disputes`. There is no resolution
    workflow/UI yet (ТЗ does not describe one either), so `resolution_status`
    starts and stays at `OPEN` until a future module closes it."""

    __tablename__ = "dispute_logs"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    process_id = Column(String(64), nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)

    inspector_comment = Column(Text, nullable=False)
    ai_comment = Column(Text, nullable=True)
    resolution_status = Column(String(32), nullable=False, server_default="OPEN", index=True)
    opened_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    evidence_group = relationship("EvidenceGroup")
    opened_by = relationship("User", foreign_keys=[opened_by_user_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_user_id])


class Suspicion(Base):
    """ТЗ-10 `Suspicions` / ТЗ 9.5's `Suspicion` JSON structure, materialized
    as a real, queryable row instead of living only inside
    `EvidenceGroup.delta` (finding_status='SUSPICION'). One row per
    SUSPICION-producing EvidenceGroup; kept in sync by whichever free-search
    discovery method produced it (currently only LOGICAL_ANALYSIS, see
    `logical_analysis.py`) and cascade-deleted when that group is swept as
    orphaned."""

    __tablename__ = "suspicions"

    id = Column(Integer, primary_key=True, index=True)
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    process_id = Column(String(64), nullable=False, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)

    discovery_method = Column(String(64), nullable=False, index=True)
    confidence = Column(Float, nullable=True)
    description = Column(Text, nullable=True)
    pd_reference = Column(String(255), nullable=True)
    rd_reference = Column(String(255), nullable=True)
    criticality = Column(String(64), nullable=True)
    normative_base = Column(Text, nullable=True)
    inspector_status = Column(String(32), nullable=False, server_default="PENDING", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    evidence_group = relationship("EvidenceGroup", back_populates="suspicion")


class LogicalRuleRecord(Base):
    """ТЗ-10 `Logical_Rules`: a DB-backed, admin-editable mirror of the
    hand-authored `LOGICAL_RULES` tuple in `logical_analysis.py`. Seeded
    (idempotently, by `rule_id`) from that tuple at startup so it always
    reflects what actually runs; the only field an admin edit here changes at
    runtime is `is_active` (checked by `run_logical_analysis_module` as an
    override of the code-level default) -- editing `condition`/`expected`
    text here is documentation only, since the condition itself is Python
    logic, not a machine-interpretable expression."""

    __tablename__ = "logical_rules"

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(String(32), unique=True, nullable=False, index=True)
    rule_name = Column(String(255), nullable=False)
    condition = Column(Text, nullable=False)
    expected = Column(Text, nullable=False)
    normative_base = Column(Text, nullable=True)
    criticality = Column(String(32), nullable=True)
    confidence = Column(Float, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="1", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class NormativeBaseEntry(Base):
    """ТЗ-10 `Normative_Base` / ТЗ module 8 ("Управление нормативной базой"):
    an admin-maintained registry of normative documents and their threshold
    values, independent of (not yet wired into) `Param.min_value`/`max_value`."""

    __tablename__ = "normative_base"

    id = Column(Integer, primary_key=True, index=True)
    document_name = Column(String(512), nullable=False)
    document_number = Column(String(128), nullable=True, index=True)
    section = Column(String(255), nullable=True, index=True)
    parameter_name = Column(String(255), nullable=True, index=True)
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)
    effective_from = Column(DateTime, nullable=True)
    effective_to = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="1", index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    created_by = relationship("User")


class MonitoringMetric(Base):
    """ТЗ-10 `Monitoring_Metrics`: a durable, queryable counterpart to the
    in-memory Prometheus counters already exposed at `/metrics` -- written for
    events that need to survive a process restart or be queried by
    (metric_name, service_name, time range), such as the ИАИС `РиН` sync
    outcome (see `iais_rin_sync.py`)."""

    __tablename__ = "monitoring_metrics"

    id = Column(Integer, primary_key=True, index=True)
    metric_name = Column(String(128), nullable=False, index=True)
    value = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    service_name = Column(String(64), nullable=False, index=True)
    tags = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class InspectorEdit(Base):
    """Phase 12 / S5 (expert session §13): append-only history of an inspector's manual changes.

    One row == one new version of one entity; rows are never updated or deleted by the application. Machine output
    (`EvidenceFragment`), source fragments/OCR and the original files are never written to -- the effective state
    an inspector sees is the machine output with these versions replayed on top (see `inspector_workbench.py`).

    entity_type
        EVIDENCE_FRAGMENT  entity_key "machine:<evidence_fragments.id>" (a machine fragment the inspector
                           refined / removed / restored) or "manual:<id of the ADD row>" (an inspector-added one)
        REVISION_CHOICE    entity_key "revision:<scope key>" (which edition of a document is authoritative)
    Each row carries the §13 fields: user_id, created_at, reason, a reference to the source entity
    (`source_fragment_id` / `source_document_version_id` / `previous_edit_id`) and the previous value."""

    __tablename__ = "inspector_edits"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    process_id = Column(String(64), ForeignKey("inspection_processes.id", ondelete="CASCADE"), nullable=False, index=True)
    object_id = Column(String(64), nullable=True, index=True)
    # SET NULL, not CASCADE: a recompute that sweeps a group must not take the inspector's history with it.
    evidence_group_id = Column(Integer, ForeignKey("evidence_groups.id", ondelete="SET NULL"), nullable=True, index=True)

    entity_type = Column(String(32), nullable=False, index=True)
    entity_key = Column(String(160), nullable=False, index=True)
    action = Column(String(32), nullable=False, index=True)
    version = Column(Integer, nullable=False)

    source_fragment_id = Column(Integer, ForeignKey("evidence_fragments.id", ondelete="SET NULL"), nullable=True, index=True)
    source_document_version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True, index=True)
    previous_edit_id = Column(Integer, ForeignKey("inspector_edits.id", ondelete="SET NULL"), nullable=True, index=True)
    previous_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    reason = Column(Text, nullable=False)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("process_id", "entity_key", "version", name="uq_inspector_edit_entity_version"),
    )
