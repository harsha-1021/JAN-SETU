# Citizen Demand AI

A multilingual Digital Public Good for collecting citizen infrastructure
requests, identifying demand hotspots, and helping policymakers prioritize
projects with transparent evidence.

## Google technology used

1. **Gemini on Vertex AI** structures every complaint and generates grounded
   policy briefs.
2. **Cloud Speech-to-Text** transcribes voice complaints.
3. **Cloud Translation** normalizes multilingual text to English for analysis.
4. **Gemini multimodal** describes relevant visual evidence in optional photos.
5. **Firebase Realtime Database** stores operational complaint/status records
   and pushes instant dashboard updates.
6. **Firebase Authentication** protects policymaker APIs and pages using a
   `policymaker` custom claim.
7. **Google Maps Platform** provides maps plus forward/reverse geocoding.
8. **BigQuery and BigQuery ML** join regional context and forecast demand
   hotspots with `ARIMA_PLUS`.
9. **Cloud Run** hosts the existing FastAPI application.

Google AI Studio can be used locally with `GEMINI_API_KEY`; the Cloud Run path
uses Gemini through Vertex AI and the service's Google identity.

## Data flow

```text
Citizen text / voice / photo
  -> Speech-to-Text + Translation
  -> Gemini structured complaint analysis
  -> Firebase live operational record
  -> BigQuery analytical record
  -> explainable priority score
  -> Google Maps policymaker hotspot dashboard
  -> Gemini evidence-grounded policy brief
```

Gemini extracts inputs and recommendations; it does not secretly determine the
priority ranking. The ranking remains the testable formula in `scoring.py`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Without Google credentials, text intake, scoring, tracking and both websites
continue to work through local fallbacks. Voice requires Google Speech or the
optional Sarvam fallback.

- Citizen site: `http://localhost:8000/citizen/`
- Policymaker login: `http://localhost:8000/dashboard/login.html`

For the complete cloud-backed demo, follow [GOOGLE_CLOUD_SETUP.md](GOOGLE_CLOUD_SETUP.md).

## Tests

```bash
python -m unittest -v
```

## Responsible-AI boundaries

- Images are treated as supporting evidence, never proof of location, time, or
  authenticity.
- The policymaker sees representative complaint summaries, not citizen contact
  details.
- Gemini recommendations are advisory and require a human decision.
- Demographic and investment data must include a source. The supplied region
  context CSV is explicitly an illustrative placeholder and must not be
  presented as official data.

