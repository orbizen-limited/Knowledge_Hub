export const CHAPTERS_QUERY = `#graphql
  query Chapters {
    chapters {
      chapter
      totalCount
      specialties {
        specialty
        count
      }
    }
  }
`;

export const SEARCH_QUERY = `#graphql
  query Search($query: String!, $limit: Int) {
    search(query: $query, limit: $limit) {
      score
      topic {
        topicId
        title
        specialty
        chapter
        bottomLine
      }
    }
  }
`;

export const TOPIC_DETAIL_QUERY = `#graphql
  query TopicDetail($topicId: ID!) {
    topic(topicId: $topicId) {
      topicId
      title
      specialty
      chapter
      tier
      bottomLine
      keywords
      careSettings
      summaryParagraphs
      recommendations { text grade source sourceUrl evidenceLevel }
      references { citation url organization year doi }
      etiologyEpidemiology
      clinicalPresentation
      differentialDiagnosis { condition distinguishingFeature }
      diagnosticWorkup
      treatmentLines { line description medicineGenericKeys }
      specialPopulations { population guidance }
      monitoringFollowUp
      complicationsPrognosis
      pathophysiology
      comorbidityManagement { heading detail }
      complicationManagement { heading detail }
      drugRegimens {
        drug indication initialDose titration maintenanceDose termination
        alternatives adverseEffectManagement monitoring genericKeys
      }
      relapseRemission
      patientEducation
      crossReferences
      backgroundInformation { heading points { text level } }
      diagnosisSections { heading points { text level } }
      managementSections { heading points { text level } }
      complicationSections { heading points { text level } }
      relatedMedicineGenericKeys
      curriculumRefs
      prognosisQuantitative { outcome estimate source doi }
      preciseDosing {
        drug indication standardDose doseReductionCriteria renalAdjustment
        hepaticAdjustment administration onsetOffset
      }
    }
  }
`;

export const TOPICS_BY_CHAPTER_QUERY = `#graphql
  query TopicsByChapter($chapter: String!, $limit: Int, $offset: Int) {
    topics(chapter: $chapter, limit: $limit, offset: $offset) {
      totalCount
      items {
        topicId
        title
        specialty
        bottomLine
      }
    }
  }
`;
