import { notFound } from 'next/navigation';
import { graphqlFetch } from '@/lib/graphqlClient';
import { TOPIC_DETAIL_QUERY } from '@/lib/queries';
import type { Topic } from '@/lib/types';
import { SectionCard, BulletList } from '@/components/SectionCard';
import { ContentBlocks } from '@/components/ContentBlocks';
import { RecordVisit } from '@/components/RecordVisit';

interface TopicPageProps {
  params: Promise<{ topicId: string }>;
}

export default async function TopicPage({ params }: TopicPageProps) {
  const { topicId } = await params;
  const { topic } = await graphqlFetch<{ topic: Topic | null }>(TOPIC_DETAIL_QUERY, { topicId });

  if (!topic) notFound();

  const sections: { id: string; title: string; colorVar: string; content: React.ReactNode }[] = [];

  if (topic.backgroundInformation.length) {
    sections.push({
      id: 'background',
      title: 'Background',
      colorVar: '--section-background',
      content: <ContentBlocks blocks={topic.backgroundInformation} />,
    });
  }
  if (topic.etiologyEpidemiology.length || topic.pathophysiology.length) {
    sections.push({
      id: 'etiology',
      title: 'Etiology, Epidemiology & Pathophysiology',
      colorVar: '--section-background',
      content: (
        <>
          {topic.etiologyEpidemiology.length > 0 && <BulletList items={topic.etiologyEpidemiology} />}
          {topic.pathophysiology.length > 0 && <BulletList items={topic.pathophysiology} />}
        </>
      ),
    });
  }
  if (topic.clinicalPresentation.length) {
    sections.push({
      id: 'presentation',
      title: 'Clinical Presentation',
      colorVar: '--section-presentation',
      content: <BulletList items={topic.clinicalPresentation} />,
    });
  }
  if (topic.diagnosisSections.length || topic.differentialDiagnosis.length || topic.diagnosticWorkup.length) {
    sections.push({
      id: 'diagnosis',
      title: 'Diagnosis',
      colorVar: '--section-diagnosis',
      content: (
        <>
          {topic.diagnosisSections.length > 0 && <ContentBlocks blocks={topic.diagnosisSections} />}
          {topic.diagnosticWorkup.length > 0 && (
            <>
              <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', margin: '12px 0 6px' }}>
                Diagnostic Workup
              </h4>
              <BulletList items={topic.diagnosticWorkup} />
            </>
          )}
          {topic.differentialDiagnosis.length > 0 && (
            <>
              <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', margin: '12px 0 6px' }}>
                Differential Diagnosis
              </h4>
              <ul style={{ paddingLeft: 20, margin: 0 }}>
                {topic.differentialDiagnosis.map((d, i) => (
                  <li key={i} style={{ marginBottom: 6 }}>
                    <strong>{d.condition}</strong>
                    {d.distinguishingFeature ? ` — ${d.distinguishingFeature}` : ''}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      ),
    });
  }
  if (topic.managementSections.length || topic.treatmentLines.length) {
    sections.push({
      id: 'management',
      title: 'Management',
      colorVar: '--section-management',
      content: (
        <>
          {topic.managementSections.length > 0 && <ContentBlocks blocks={topic.managementSections} />}
          {topic.treatmentLines.length > 0 && (
            <ul style={{ paddingLeft: 20, margin: '12px 0 0' }}>
              {topic.treatmentLines.map((t, i) => (
                <li key={i} style={{ marginBottom: 8 }}>
                  <strong>{t.line}</strong> — {t.description}
                </li>
              ))}
            </ul>
          )}
        </>
      ),
    });
  }
  if (topic.drugRegimens.length) {
    sections.push({
      id: 'drugs',
      title: 'Drug Regimens',
      colorVar: '--section-drugs',
      content: (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {topic.drugRegimens.map((d, i) => (
            <div key={i} style={{ borderBottom: i === topic.drugRegimens.length - 1 ? 'none' : '1px solid var(--border)', paddingBottom: 10 }}>
              <div style={{ fontWeight: 600 }}>{d.drug}</div>
              {d.indication && <div style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>{d.indication}</div>}
              {d.initialDose && <div style={{ marginTop: 4 }}><span className="mono" style={{ fontSize: '0.75rem' }}>Initial: </span>{d.initialDose}</div>}
              {d.maintenanceDose && <div><span className="mono" style={{ fontSize: '0.75rem' }}>Maintenance: </span>{d.maintenanceDose}</div>}
              {d.monitoring && <div><span className="mono" style={{ fontSize: '0.75rem' }}>Monitoring: </span>{d.monitoring}</div>}
            </div>
          ))}
        </div>
      ),
    });
  }
  if (topic.preciseDosing.length) {
    sections.push({
      id: 'precise-dosing',
      title: 'Precise Dosing',
      colorVar: '--section-drugs',
      content: (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {topic.preciseDosing.map((d, i) => (
            <div key={i}>
              <div style={{ fontWeight: 600 }}>{d.drug}</div>
              {d.standardDose && <div>{d.standardDose}</div>}
              {d.renalAdjustment && <div className="mono" style={{ fontSize: '0.8rem' }}>Renal: {d.renalAdjustment}</div>}
              {d.hepaticAdjustment && <div className="mono" style={{ fontSize: '0.8rem' }}>Hepatic: {d.hepaticAdjustment}</div>}
            </div>
          ))}
        </div>
      ),
    });
  }
  if (topic.specialPopulations.length) {
    sections.push({
      id: 'special-populations',
      title: 'Special Populations',
      colorVar: '--section-special-populations',
      content: (
        <ul style={{ paddingLeft: 20, margin: 0 }}>
          {topic.specialPopulations.map((s, i) => (
            <li key={i} style={{ marginBottom: 6 }}>
              <strong>{s.population}</strong> — {s.guidance}
            </li>
          ))}
        </ul>
      ),
    });
  }
  if (topic.comorbidityManagement.length) {
    sections.push({
      id: 'comorbidities',
      title: 'Comorbidity Management',
      colorVar: '--section-special-populations',
      content: (
        <ul style={{ paddingLeft: 20, margin: 0 }}>
          {topic.comorbidityManagement.map((k, i) => (
            <li key={i} style={{ marginBottom: 6 }}>
              <strong>{k.heading}</strong> — {k.detail}
            </li>
          ))}
        </ul>
      ),
    });
  }
  if (topic.monitoringFollowUp.length) {
    sections.push({
      id: 'monitoring',
      title: 'Monitoring & Follow-up',
      colorVar: '--section-monitoring',
      content: <BulletList items={topic.monitoringFollowUp} />,
    });
  }
  if (
    topic.complicationSections.length ||
    topic.complicationManagement.length ||
    topic.complicationsPrognosis.length
  ) {
    sections.push({
      id: 'complications',
      title: 'Complications & Prognosis',
      colorVar: '--section-complications',
      content: (
        <>
          {topic.complicationSections.length > 0 && <ContentBlocks blocks={topic.complicationSections} />}
          {topic.complicationManagement.length > 0 && (
            <ul style={{ paddingLeft: 20, margin: '12px 0 0' }}>
              {topic.complicationManagement.map((k, i) => (
                <li key={i} style={{ marginBottom: 6 }}>
                  <strong>{k.heading}</strong> — {k.detail}
                </li>
              ))}
            </ul>
          )}
          {topic.complicationsPrognosis.length > 0 && (
            <BulletList items={topic.complicationsPrognosis} />
          )}
        </>
      ),
    });
  }
  if (topic.prognosisQuantitative.length) {
    sections.push({
      id: 'prognosis-quantitative',
      title: 'Prognosis — Quantitative Outcomes',
      colorVar: '--section-complications',
      content: (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.9rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--text-secondary)' }}>
              <th style={{ paddingBottom: 6 }}>Outcome</th>
              <th style={{ paddingBottom: 6 }}>Estimate</th>
              <th style={{ paddingBottom: 6 }}>Source</th>
            </tr>
          </thead>
          <tbody>
            {topic.prognosisQuantitative.map((p, i) => (
              <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '6px 0' }}>{p.outcome}</td>
                <td style={{ padding: '6px 0' }}>{p.estimate}</td>
                <td style={{ padding: '6px 0', color: 'var(--text-muted)' }}>{p.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ),
    });
  }
  if (topic.relapseRemission.length) {
    sections.push({
      id: 'relapse',
      title: 'Relapse & Remission',
      colorVar: '--section-management',
      content: <BulletList items={topic.relapseRemission} />,
    });
  }
  if (topic.patientEducation.length) {
    sections.push({
      id: 'education',
      title: 'Patient Education',
      colorVar: '--section-education',
      content: <BulletList items={topic.patientEducation} />,
    });
  }
  if (topic.recommendations.length) {
    sections.push({
      id: 'recommendations',
      title: 'Recommendations',
      colorVar: '--section-recommendations',
      content: (
        <ul style={{ paddingLeft: 20, margin: 0 }}>
          {topic.recommendations.map((r, i) => (
            <li key={i} style={{ marginBottom: 8 }}>
              <span className="chip" style={{ marginRight: 8 }}>
                Grade {r.grade}
              </span>
              {r.text}
              {r.source && <span style={{ color: 'var(--text-muted)' }}> ({r.source})</span>}
            </li>
          ))}
        </ul>
      ),
    });
  }
  if (topic.crossReferences.length || topic.relatedMedicineGenericKeys.length) {
    sections.push({
      id: 'see-also',
      title: 'See Also',
      colorVar: '--section-see-also',
      content: (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {topic.crossReferences.map((c, i) => (
            <span key={`x-${i}`} className="chip">
              {c}
            </span>
          ))}
          {topic.relatedMedicineGenericKeys.map((c, i) => (
            <span key={`m-${i}`} className="chip">
              💊 {c}
            </span>
          ))}
        </div>
      ),
    });
  }
  if (topic.references.length) {
    sections.push({
      id: 'references',
      title: 'References',
      colorVar: '--section-references',
      content: (
        <ol style={{ paddingLeft: 20, margin: 0 }}>
          {topic.references.map((r, i) => (
            <li key={i} style={{ marginBottom: 6, fontSize: '0.9rem' }}>
              {r.url ? (
                <a href={r.url} target="_blank" rel="noopener noreferrer nofollow">
                  {r.citation}
                </a>
              ) : (
                r.citation
              )}
              {r.organization && ` — ${r.organization}`}
              {r.year > 0 && ` (${r.year})`}
            </li>
          ))}
        </ol>
      ),
    });
  }

  return (
    <main style={{ maxWidth: 820, margin: '0 auto', padding: '32px 24px 80px' }}>
      <RecordVisit
        topicId={topic.topicId}
        title={topic.title}
        specialty={topic.specialty}
        chapter={topic.chapter}
      />

      <article>
        <header style={{ marginBottom: 24 }}>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
            <span className="chip">{topic.specialty}</span>
            {topic.chapter && <span className="chip">{topic.chapter}</span>}
            <span className="chip">{topic.tier.toUpperCase()}</span>
            {topic.careSettings.map((c) => (
              <span key={c} className="chip">
                {c}
              </span>
            ))}
          </div>
          <h1 style={{ fontSize: '1.8rem' }}>{topic.title}</h1>
          {topic.bottomLine && (
            <p className="card" style={{ padding: '16px 20px', color: 'var(--text-primary)', fontSize: '1.02rem' }}>
              {topic.bottomLine}
            </p>
          )}
          {topic.summaryParagraphs.map((p, i) => (
            <p key={i} style={{ color: 'var(--text-secondary)' }}>
              {p}
            </p>
          ))}
        </header>

        {sections.length > 1 && (
          <nav
            aria-label="Jump to section"
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 8,
              marginBottom: 24,
              position: 'sticky',
              top: 0,
              background: 'var(--bg-base)',
              paddingTop: 8,
              paddingBottom: 8,
              zIndex: 5,
            }}
          >
            {sections.map((s) => (
              <a
                key={s.id}
                href={`#${s.id}`}
                className="chip"
                style={{ color: `var(${s.colorVar})`, borderColor: `var(${s.colorVar})` }}
              >
                {s.title}
              </a>
            ))}
          </nav>
        )}

        {sections.map((s) => (
          <SectionCard key={s.id} id={s.id} title={s.title} colorVar={s.colorVar}>
            {s.content}
          </SectionCard>
        ))}
      </article>
    </main>
  );
}
