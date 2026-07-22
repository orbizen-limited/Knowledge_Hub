export interface Recommendation {
  text: string;
  grade: string;
  source: string;
  sourceUrl: string;
  evidenceLevel: string;
}

export interface Reference {
  citation: string;
  url: string;
  organization: string;
  year: number;
  doi: string;
}

export interface DifferentialDiagnosisEntry {
  condition: string;
  distinguishingFeature: string;
}

export interface TreatmentLine {
  line: string;
  description: string;
  medicineGenericKeys: string[];
}

export interface SpecialPopulationNote {
  population: string;
  guidance: string;
}

export interface KeyedDetail {
  heading: string;
  detail: string;
}

export interface DrugRegimen {
  drug: string;
  indication: string;
  initialDose: string;
  titration: string;
  maintenanceDose: string;
  termination: string;
  alternatives: string;
  adverseEffectManagement: string;
  monitoring: string;
  genericKeys: string[];
}

export interface ContentPoint {
  text: string;
  level: number;
}

export interface ContentBlock {
  heading: string;
  points: ContentPoint[];
}

export interface PrognosisQuantitativeEntry {
  outcome: string;
  estimate: string;
  source: string;
  doi: string;
}

export interface PreciseDosingEntry {
  drug: string;
  indication: string;
  standardDose: string;
  doseReductionCriteria: string;
  renalAdjustment: string;
  hepaticAdjustment: string;
  administration: string;
  onsetOffset: string;
}

export interface Topic {
  topicId: string;
  title: string;
  specialty: string;
  chapter: string;
  tier: string;
  bottomLine: string;
  keywords: string[];
  careSettings: string[];
  summaryParagraphs: string[];
  recommendations: Recommendation[];
  references: Reference[];
  etiologyEpidemiology: string[];
  clinicalPresentation: string[];
  differentialDiagnosis: DifferentialDiagnosisEntry[];
  diagnosticWorkup: string[];
  treatmentLines: TreatmentLine[];
  specialPopulations: SpecialPopulationNote[];
  monitoringFollowUp: string[];
  complicationsPrognosis: string[];
  pathophysiology: string[];
  comorbidityManagement: KeyedDetail[];
  complicationManagement: KeyedDetail[];
  drugRegimens: DrugRegimen[];
  relapseRemission: string[];
  patientEducation: string[];
  crossReferences: string[];
  backgroundInformation: ContentBlock[];
  diagnosisSections: ContentBlock[];
  managementSections: ContentBlock[];
  complicationSections: ContentBlock[];
  relatedMedicineGenericKeys: string[];
  curriculumRefs: string[];
  prognosisQuantitative: PrognosisQuantitativeEntry[];
  preciseDosing: PreciseDosingEntry[];
}

export interface SearchResult {
  score: number;
  topic: Pick<Topic, 'topicId' | 'title' | 'specialty' | 'chapter' | 'bottomLine'>;
}

export interface ChapterSummary {
  chapter: string;
  totalCount: number;
  specialties: { specialty: string; count: number }[];
}
