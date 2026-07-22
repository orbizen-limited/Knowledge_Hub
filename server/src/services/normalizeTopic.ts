// Mirrors the tolerant parsing rules in doctorshero-rx's
// lib/models/knowledge_hub/knowledge_topic.dart. A meaningful slice of the
// authored content pack writes fields in alternate shapes (prose string
// instead of a list, a Map instead of a List, numbers as strings, etc.) —
// this normalizes every topic into the canonical shape the GraphQL schema
// expects, the same way the Flutter app's fromJson factories do, so nothing
// throws at query time and no content is silently dropped.

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

function isPlainObject(v: Json): v is Record<string, Json> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function rawList(raw: Json): Json[] {
  if (Array.isArray(raw)) return raw;
  if (isPlainObject(raw)) return [raw];
  return [];
}

function mapList(raw: Json): Record<string, Json>[] {
  return rawList(raw).filter(isPlainObject);
}

function stringOr(raw: Json, fallback = ''): string {
  if (raw === null || raw === undefined) return fallback;
  return typeof raw === 'string' ? raw : String(raw);
}

function intOr(raw: Json, fallback: number): number {
  if (typeof raw === 'number') return Math.trunc(raw);
  if (typeof raw === 'string') {
    const parsed = Number.parseInt(raw.trim(), 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  return fallback;
}

function stringList(raw: Json): string[] {
  if (raw === null || raw === undefined) return [];
  if (typeof raw === 'string') {
    const t = raw.trim();
    return t ? [t] : [];
  }
  if (Array.isArray(raw)) {
    return raw.map((p) => String(p).trim()).filter((p) => p.length > 0);
  }
  return [];
}

interface ContentPoint {
  text: string;
  level: number;
}
interface ContentBlock {
  heading: string;
  points: ContentPoint[];
}

function walkPoints(raw: Json, level: number, out: ContentPoint[]): void {
  if (Array.isArray(raw)) {
    for (const e of raw) walkPoints(e, level, out);
    return;
  }
  if (typeof raw === 'string') {
    if (raw.trim()) out.push({ text: raw, level });
    return;
  }
  if (isPlainObject(raw)) {
    const lvl = typeof raw.level === 'number' ? raw.level : level;
    const text = stringOr(raw.text ?? raw.point ?? raw.detail, '');
    if (text.trim()) out.push({ text, level: lvl });
    for (const childKey of ['children', 'subpoints', 'sub']) {
      if (raw[childKey] != null) walkPoints(raw[childKey], lvl + 1, out);
    }
  }
}

function blockFromJson(json: Record<string, Json>): ContentBlock {
  const points: ContentPoint[] = [];
  walkPoints(json.points ?? json.bullets ?? json.items ?? [], 0, points);
  return { heading: stringOr(json.heading ?? json.title, ''), points };
}

function parseContentBlocks(raw: Json, fallbackHeading: string): ContentBlock[] {
  if (raw === null || raw === undefined) return [];

  if (typeof raw === 'string') {
    const text = raw.trim();
    if (!text) return [];
    return [{ heading: fallbackHeading, points: [{ text, level: 0 }] }];
  }

  if (isPlainObject(raw)) {
    const blocks: ContentBlock[] = [];
    for (const [key, value] of Object.entries(raw)) {
      const heading = key
        .replace(/_/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .trim();
      if (typeof value === 'string') {
        if (value.trim()) blocks.push({ heading, points: [{ text: value, level: 0 }] });
      } else if (Array.isArray(value)) {
        const parsed = blockFromJson({ heading, points: value });
        if (parsed.heading.trim() || parsed.points.length) blocks.push(parsed);
      } else if (isPlainObject(value)) {
        const parsed = blockFromJson({ heading, ...value });
        if (parsed.heading.trim() || parsed.points.length) blocks.push(parsed);
      }
    }
    return blocks;
  }

  if (Array.isArray(raw)) {
    const blocks: ContentBlock[] = [];
    for (const e of raw) {
      if (typeof e === 'string') {
        if (e.trim()) blocks.push({ heading: '', points: [{ text: e, level: 0 }] });
      } else if (isPlainObject(e)) {
        blocks.push(blockFromJson(e));
      }
    }
    return blocks;
  }

  return [];
}

const TIER_NAMES = ['tier1', 'tier2', 'tier3'];
const REVIEW_STATUS_NAMES = [
  'pending_clinician_check',
  'pending_board_review',
  'approved',
  'rejected',
];
const CARE_SETTING_NAMES = ['outpatient', 'inpatient', 'critical'];

function parseDate(raw: Json): Date | null {
  const str = stringOr(raw, '');
  if (!str) return null;
  const d = new Date(str);
  return Number.isNaN(d.getTime()) ? null : d;
}

export interface NormalizedTopic {
  topicId: string;
  title: string;
  specialty: string;
  chapter: string;
  tier: string;
  contentVersion: number;
  lastUpdated: Date | null;
  reviewStatus: string;
  reviewedBy: string;
  reviewedAt: Date | null;
  bottomLine: string;
  agentGenerated: boolean;
  keywords: string[];
  careSettings: string[];
  summaryParagraphs: string[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  content: Record<string, any>;
}

export function normalizeTopic(json: Record<string, Json>): NormalizedTopic {
  const tierIndex = intOr(json.tier, 0);
  const tier = TIER_NAMES[tierIndex] ?? 'tier2';

  const reviewStatusRaw = json.reviewStatus;
  const reviewStatusIndex = reviewStatusRaw === null || reviewStatusRaw === undefined
    ? -1
    : intOr(reviewStatusRaw, -1);
  const reviewStatus =
    reviewStatusIndex >= 0 && reviewStatusIndex < REVIEW_STATUS_NAMES.length
      ? REVIEW_STATUS_NAMES[reviewStatusIndex]
      : 'approved';

  const careSettings = rawList(json.careSettings)
    .filter((s): s is number => typeof s === 'number' && s >= 0 && s < CARE_SETTING_NAMES.length)
    .map((s) => CARE_SETTING_NAMES[s]);

  return {
    topicId: stringOr(json.topicId, ''),
    title: stringOr(json.title, ''),
    specialty: stringOr(json.specialty, ''),
    chapter: stringOr(json.chapter, ''),
    tier,
    contentVersion: intOr(json.contentVersion, 1),
    lastUpdated: parseDate(json.lastUpdated),
    reviewStatus,
    reviewedBy: stringOr(json.reviewedBy, ''),
    reviewedAt: parseDate(json.reviewedAt),
    bottomLine: stringOr(json.bottomLine ?? json.clinicalBottomLine, ''),
    agentGenerated: Boolean(json.agentGenerated ?? false),
    keywords: stringList(json.keywords),
    careSettings,
    summaryParagraphs: stringList(json.summaryParagraphs ?? json.summary),
    content: {
      recommendations: mapList(json.recommendations).map((r) => ({
        text: stringOr(r.text, ''),
        grade: stringOr(r.grade, 'B'),
        source: stringOr(r.source, ''),
        sourceUrl: stringOr(r.sourceUrl, ''),
        evidenceLevel: stringOr(r.evidenceLevel, ''),
      })),
      references: mapList(json.references).map((r) => ({
        citation: stringOr(r.citation ?? r.title, ''),
        url: stringOr(r.url, ''),
        organization: stringOr(r.organization, ''),
        year: intOr(r.year, 0),
        doi: stringOr(r.doi, ''),
      })),
      etiologyEpidemiology: stringList(json.etiologyEpidemiology),
      clinicalPresentation: stringList(json.clinicalPresentation),
      differentialDiagnosis: mapList(json.differentialDiagnosis).map((d) => ({
        condition: stringOr(d.condition, ''),
        distinguishingFeature: stringOr(d.distinguishingFeature, ''),
      })),
      diagnosticWorkup: stringList(json.diagnosticWorkup),
      treatmentLines: mapList(json.treatmentLines).map((t) => ({
        line: stringOr(t.line, ''),
        description: stringOr(t.description, ''),
        medicineGenericKeys: stringList(t.medicineGenericKeys),
      })),
      specialPopulations: mapList(json.specialPopulations).map((s) => ({
        population: stringOr(s.population, ''),
        guidance: stringOr(s.guidance ?? s.considerations ?? s.notes, ''),
      })),
      monitoringFollowUp: stringList(json.monitoringFollowUp),
      complicationsPrognosis: stringList(json.complicationsPrognosis),
      pathophysiology: stringList(json.pathophysiology),
      comorbidityManagement: mapList(json.comorbidityManagement).map((e) => ({
        heading: stringOr(e.heading ?? e.condition ?? e.complication ?? e.comorbidity, ''),
        detail: stringOr(e.detail ?? e.management ?? e.guidance, ''),
      })),
      complicationManagement: mapList(json.complicationManagement).map((e) => ({
        heading: stringOr(e.heading ?? e.condition ?? e.complication ?? e.comorbidity, ''),
        detail: stringOr(e.detail ?? e.management ?? e.guidance, ''),
      })),
      drugRegimens: mapList(json.drugRegimens).map((d) => ({
        drug: stringOr(d.drug ?? d.name, ''),
        indication: stringOr(d.indication, ''),
        initialDose: stringOr(d.initialDose ?? d.initial ?? d.dose, ''),
        titration: stringOr(d.titration, ''),
        maintenanceDose: stringOr(d.maintenanceDose ?? d.maintenance, ''),
        termination: stringOr(d.termination ?? d.taper ?? d.stop, ''),
        alternatives: stringOr(d.alternatives, ''),
        adverseEffectManagement: stringOr(d.adverseEffectManagement ?? d.adverseEffects, ''),
        monitoring: stringOr(d.monitoring, ''),
        genericKeys: rawList(d.genericKeys ?? d.medicineGenericKeys).map((k) => String(k)),
      })),
      relapseRemission: stringList(json.relapseRemission),
      patientEducation: stringList(json.patientEducation),
      crossReferences: stringList(json.crossReferences),
      backgroundInformation: parseContentBlocks(json.backgroundInformation, 'Background'),
      diagnosisSections: parseContentBlocks(json.diagnosisSections, 'Diagnosis'),
      managementSections: parseContentBlocks(json.managementSections, 'Management'),
      complicationSections: parseContentBlocks(json.complicationSections, 'Complications'),
      relatedMedicineGenericKeys: stringList(json.relatedMedicineGenericKeys),
      curriculumRefs: stringList(json.curriculumRefs),
      prognosisQuantitative: mapList(json.prognosisQuantitative).map((p) => ({
        outcome: stringOr(p.outcome, ''),
        estimate: stringOr(p.estimate, ''),
        source: stringOr(p.source, ''),
        doi: stringOr(p.doi, ''),
      })),
      preciseDosing: mapList(json.preciseDosing).map((p) => ({
        drug: stringOr(p.drug, ''),
        indication: stringOr(p.indication, ''),
        standardDose: stringOr(p.standardDose, ''),
        doseReductionCriteria: stringOr(p.doseReductionCriteria, ''),
        renalAdjustment: stringOr(p.renalAdjustment, ''),
        hepaticAdjustment: stringOr(p.hepaticAdjustment, ''),
        administration: stringOr(p.administration, ''),
        onsetOffset: stringOr(p.onsetOffset, ''),
      })),
      reviewLog: stringList(json.reviewLog),
    },
  };
}
