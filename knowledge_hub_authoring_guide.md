# Offline Knowledge Hub Authoring Guide

Yes, you can absolutely develop and enrich topics offline or elsewhere as JSON files, and then load them directly into the app. The Knowledge Hub is designed to seamlessly ingest any JSON file that matches its expected schema.

Here is a complete guide on how to do this safely and cleanly, including the exact absolute file paths on your Mac so you can feed them directly into an external AI (like ChatGPT or Gemini).

## 1. Where are the AI Skills and Prompts?

If you want to use an LLM outside of the IDE to generate the JSON, you MUST provide it with the exact system prompt and the two medical quality guidelines used in this project.

*   **Skill 1: Medical Content Quality Framework**
    *   **Exact Path:** `/Users/helal/doctorshero-rx/skills/medical-content-quality-framework/SKILL.md`
    *   *Purpose:* This defines the medical quality standard, the 23 required canonical sections, and the strict rules on evidence, grading, and non-fabrication.

*   **Skill 2: Knowledge Hub Operational Guide**
    *   **Exact Path:** `/Users/helal/doctorshero-rx/skills/doctorshero-knowledge-hub/SKILL.md`
    *   *Purpose:* This tells the AI how to build the file, detailing the character minimums (100k+) and the schema formatting rules.

*   **JSON Schema & Formatting Prompt:**
    *   **Exact Path:** `/Users/helal/doctorshero-rx/tools/tier1_enhance/prompts/master_topic_prompt.md`
    *   *Purpose:* This provides the exact JSON output schema, ensuring that complex objects like `drugRegimens` and nested arrays are perfectly shaped for the Flutter UI.

**What to paste in your external AI chat:**
> "I am authoring a medical topic for the DoctorsHero Knowledge Hub. Please read these three guideline files: 
> 1. `medical-content-quality-framework/SKILL.md` 
> 2. `doctorshero-knowledge-hub/SKILL.md`
> 3. `master_topic_prompt.md` 
> 
> Read these carefully, and then output my requested topic perfectly matching this JSON schema and the v2.1 quality standard."

## 2. File Structure

The AI should output a single valid JSON object. Save it as a `.json` file using a lowercase slugified name. 
Example: `nephrology-acute_kidney_injury.json`

## 3. How to Load It Into the App

Once you have your generated JSON file, follow these 3 exact steps to load it into the app:

### Step A: Place the file in the correct directory
Copy your JSON file into this exact directory:
`/Users/helal/doctorshero-rx/assets/knowledge_hub/enriched/`

### Step B: Register the file in the loader
Open the following Dart file in your editor:
`/Users/helal/doctorshero-rx/lib/services/knowledge_hub/enriched_topics_loader.dart`

Scroll down to the `_assetFiles` list (around line 22) and insert your new file's path alphabetically into the list:
```dart
  static const _assetFiles = [
    // ... other files
    'assets/knowledge_hub/enriched/my-topic.json',
    // ...
```

### Step C: Verify pubspec.yaml (Optional check)
In Flutter, if you declare a folder like `assets/knowledge_hub/enriched/` in your `pubspec.yaml`, all files inside it are automatically included. If your file still isn't loading, ensure that path is listed under `assets:` in:
`/Users/helal/doctorshero-rx/pubspec.yaml`

## 4. Rebuild the App

Once the file is placed in the folder and registered in `enriched_topics_loader.dart`, simply perform a full hot restart of your app (or run `flutter clean && flutter run -d macos`). The app's startup logic will automatically parse your new JSON, convert it into a `KnowledgeTopic` model, and inject it into the catalog for immediate offline viewing!
