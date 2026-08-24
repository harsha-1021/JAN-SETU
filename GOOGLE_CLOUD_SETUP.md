# Google Cloud setup

This setup makes every Google integration in the application active. Use one
Google Cloud/Firebase project for the hackathon demo.

## 1. Create and select the project

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
```

Enable billing, then enable the APIs:

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  speech.googleapis.com \
  translate.googleapis.com \
  bigquery.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  identitytoolkit.googleapis.com \
  firebasedatabase.googleapis.com \
  maps-backend.googleapis.com \
  geocoding-backend.googleapis.com
```

## 2. Configure Firebase

In Firebase Console:

1. Add Firebase to the same Google Cloud project.
2. Create a Realtime Database. Singapore is a practical nearby region when an
   India RTDB location is unavailable.
3. Enable Email/Password under Authentication -> Sign-in method.
4. Register a Web app and copy its configuration values into `.env`.
5. Install Firebase CLI and deploy the included database rules:

```bash
firebase login
firebase use YOUR_PROJECT_ID
firebase deploy --only database
```

Create the first policymaker account and its authorization claim:

```bash
python create_firebase_policymaker.py
```

The user must sign out and back in after any custom-claim change so Firebase
issues a fresh ID token.

## 3. Configure Google Maps

Create two API keys:

- Browser key: allow Maps JavaScript API and restrict it by your Cloud Run URL
  and localhost HTTP referrers.
- Server key: allow Geocoding API and restrict it to the backend environment as
  tightly as the demo setup permits.

Set `GOOGLE_MAPS_BROWSER_API_KEY` and `GOOGLE_MAPS_SERVER_API_KEY`. Never commit
either key.

## 4. Create BigQuery resources

```bash
python setup_google_cloud.py
```

Load a sourced region-context CSV with the same columns as the example:

```bash
python setup_google_cloud.py --context-csv /path/to/official_region_context.csv
```

For UI testing only, the explicitly labelled illustrative file can be loaded:

```bash
python setup_google_cloud.py --load-example-context
```

Migrate existing SQLite demo complaints after Firebase and BigQuery exist:

```bash
python sync_local_data.py
```

After enough historical coverage exists, replace `PROJECT_ID` and `DATASET_ID`
in `bigquery/create_forecast_model.sql` and run it in BigQuery. The dashboard
will automatically consume the resulting `hotspot_forecast` model.

## 5. Run the full integration locally

Export the variables in `.env.example`, then run:

```bash
uvicorn main:app --reload
```

The `/config/public` endpoint confirms which browser integrations are enabled.
The actual AI API calls use Application Default Credentials.

## 6. Deploy to Cloud Run

Create a dedicated runtime identity:

```bash
gcloud iam service-accounts create citizen-demand-run
```

Grant only the project roles needed for the demo: Vertex AI user, Speech
client, Cloud Translation user, BigQuery job user/data editor, and Firebase
access. Attach that service account during deployment.

Deploy from source:

```bash
gcloud run deploy citizen-demand-ai \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --service-account citizen-demand-run@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,BIGQUERY_DATASET=citizen_demand,BIGQUERY_LOCATION=asia-south1
```

Add the Firebase and Maps configuration as Cloud Run environment variables.
Prefer Secret Manager for sensitive runtime values. Vertex AI on Cloud Run
does not require a Gemini API key.

The Cloud Run service itself is public so citizens can submit and track
complaints. Policymaker API routes remain protected by verified Firebase ID
tokens and the `policymaker` custom claim.

## 7. Hackathon demonstration sequence

1. Submit a voice complaint in an Indian language.
2. Attach a photo of the infrastructure issue.
3. Show the Gemini-generated structured understanding and tracking code.
4. Open the policymaker dashboard and show the Firebase instant update.
5. Show the complaint on Google Maps with BigQuery regional context.
6. Display the BigQuery ML hotspot forecast.
7. Generate the Gemini policy brief and then forward the priority.
8. Update the escalation and show the citizen tracker change.

