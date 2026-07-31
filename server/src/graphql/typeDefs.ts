export const typeDefs = `#graphql
  type Recommendation {
    text: String!
    grade: String!
    source: String!
    sourceUrl: String!
    evidenceLevel: String!
  }

  type Reference {
    citation: String!
    url: String!
    organization: String!
    year: Int!
    doi: String!
  }

  type DifferentialDiagnosisEntry {
    condition: String!
    distinguishingFeature: String!
  }

  type TreatmentLine {
    line: String!
    description: String!
    medicineGenericKeys: [String!]!
  }

  type SpecialPopulationNote {
    population: String!
    guidance: String!
  }

  type KeyedDetail {
    heading: String!
    detail: String!
  }

  type RenalAdjustmentBand {
    egfrBand: String!
    action: String!
  }

  type HepaticAdjustmentBand {
    severityBand: String!
    action: String!
  }

  type DoseSpec {
    amount: Float!
    unit: String!
    route: String!
    frequency: String!
    durationDays: Int
    maxDosePerDay: String!
    taperSchedule: String
    renalAdjustment: [RenalAdjustmentBand!]!
    hepaticAdjustment: [HepaticAdjustmentBand!]!
    genericKey: String!
    refIds: [Int!]!
  }

  type DrugInteractionFlag {
    type: String!
    subject: String!
    action: String!
    severity: String
    refIds: [Int!]!
  }

  type FacetAnchor {
    facetTopicId: String!
    sectionHeading: String!
  }

  type DrugRegimen {
    drug: String!
    indication: String!
    initialDose: String!
    titration: String!
    maintenanceDose: String!
    termination: String!
    alternatives: String!
    adverseEffectManagement: String!
    monitoring: String!
    genericKeys: [String!]!
    doseSpec: DoseSpec
  }

  type ContentPoint {
    text: String!
    level: Int!
  }

  type ContentBlock {
    heading: String!
    points: [ContentPoint!]!
  }

  type PrognosisQuantitativeEntry {
    outcome: String!
    estimate: String!
    source: String!
    doi: String!
  }

  type PreciseDosingEntry {
    drug: String!
    indication: String!
    standardDose: String!
    doseReductionCriteria: String!
    renalAdjustment: String!
    hepaticAdjustment: String!
    administration: String!
    onsetOffset: String!
  }

  type Topic {
    topicId: ID!
    title: String!
    specialty: String!
    chapter: String!
    tier: String!
    contentVersion: Int!
    lastUpdated: String
    reviewStatus: String!
    reviewedBy: String!
    reviewedAt: String
    reviewLog: [String!]!
    bottomLine: String!
    agentGenerated: Boolean!
    keywords: [String!]!
    careSettings: [String!]!
    summaryParagraphs: [String!]!
    recommendations: [Recommendation!]!
    references: [Reference!]!
    etiologyEpidemiology: [String!]!
    clinicalPresentation: [String!]!
    differentialDiagnosis: [DifferentialDiagnosisEntry!]!
    diagnosticWorkup: [String!]!
    treatmentLines: [TreatmentLine!]!
    specialPopulations: [SpecialPopulationNote!]!
    monitoringFollowUp: [String!]!
    complicationsPrognosis: [String!]!
    pathophysiology: [String!]!
    comorbidityManagement: [KeyedDetail!]!
    complicationManagement: [KeyedDetail!]!
    drugRegimens: [DrugRegimen!]!
    relapseRemission: [String!]!
    patientEducation: [String!]!
    crossReferences: [String!]!
    backgroundInformation: [ContentBlock!]!
    diagnosisSections: [ContentBlock!]!
    managementSections: [ContentBlock!]!
    complicationSections: [ContentBlock!]!
    relatedMedicineGenericKeys: [String!]!
    curriculumRefs: [String!]!
    prognosisQuantitative: [PrognosisQuantitativeEntry!]!
    preciseDosing: [PreciseDosingEntry!]!
    contentStandard: String!
    referenceStyle: String!
    canonicalTopicId: String!
    facetAnchors: [FacetAnchor!]!
    drugInteractionFlags: [DrugInteractionFlag!]!
  }

  type TopicConnection {
    totalCount: Int!
    items: [Topic!]!
  }

  type SearchResult {
    topic: Topic!
    score: Float!
  }

  type SpecialtyCount {
    specialty: String!
    count: Int!
  }

  type ChapterSummary {
    chapter: String!
    specialties: [SpecialtyCount!]!
    totalCount: Int!
  }

  type HealthStatus {
    status: String!
    topicCount: Int!
  }

  type SpecialtyStats {
    specialty: String!
    total: Int!
    enriched: Int!
    stub: Int!
    pendingReview: Int!
    rejected: Int!
  }

  type KnowledgeHubStats {
    total: Int!
    enriched: Int!
    stub: Int!
    pendingReview: Int!
    rejected: Int!
    bySpecialty: [SpecialtyStats!]!
  }

  type Query {
    topic(topicId: ID!): Topic
    topics(
      specialty: String
      chapter: String
      tier: String
      careSetting: String
      status: String
      search: String
      limit: Int = 20
      offset: Int = 0
    ): TopicConnection!
    search(query: String!, limit: Int = 20): [SearchResult!]!
    chapters: [ChapterSummary!]!
    stats: KnowledgeHubStats!
    health: HealthStatus!
  }
`;
