"""Google AI and Google Cloud adapters used by the complaint pipeline.

Every client is initialized lazily.  That keeps local development and unit
tests usable without cloud credentials while making Google services the
primary path whenever the related environment variables are configured.
"""

import os
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

import scoring


class ComplaintAnalysis(BaseModel):
    category: str
    severity_score: float = Field(ge=0.0, le=1.0)
    summary: str
    infrastructure_need: str
    responsible_department: str
    confidence: float = Field(ge=0.0, le=1.0)
    image_evidence: Optional[str] = None
    provider: str = "local-fallback"


class _GeminiComplaintAnalysis(BaseModel):
    category: str = Field(description="One of health, water, sanitation, roads, power, education, other")
    severity_score: float = Field(ge=0.0, le=1.0)
    summary: str = Field(description="A neutral one-sentence summary without personal data")
    infrastructure_need: str = Field(description="The concrete public infrastructure need")
    responsible_department: str = Field(description="Likely government department")
    confidence: float = Field(ge=0.0, le=1.0)
    image_evidence: Optional[str] = Field(
        default=None,
        description="Visible infrastructure evidence in the photo, without inferring identities",
    )


class PolicyBrief(BaseModel):
    title: str
    recommended_project: str
    rationale: str
    expected_impact: str
    first_action: str
    risks_and_safeguards: List[str]
    provider: str = "gemini"


class GoogleCloudServices:
    def __init__(self) -> None:
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        self.location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
        self.gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
        self.gemini_api_key = (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        ).strip()
        legacy_maps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
        self.maps_browser_api_key = os.environ.get(
            "GOOGLE_MAPS_BROWSER_API_KEY", legacy_maps_key
        ).strip()
        self.maps_server_api_key = os.environ.get(
            "GOOGLE_MAPS_SERVER_API_KEY", legacy_maps_key
        ).strip()
        self._gemini_client = None
        self._gemini_initialized = False

    @property
    def gemini_configured(self) -> bool:
        return bool(self.project_id or self.gemini_api_key)

    @property
    def cloud_apis_configured(self) -> bool:
        return bool(self.project_id)

    @property
    def gemini_mode(self) -> str:
        if self.gemini_api_key:
            return "Gemini Developer API"
        if self.project_id:
            return "Vertex AI"
        return "local fallback"

    def _get_gemini_client(self):
        if self._gemini_initialized:
            return self._gemini_client
        self._gemini_initialized = True
        if not self.gemini_configured:
            return None
        try:
            from google import genai

            # A local AI Studio key takes precedence when present. Cloud Run
            # omits the key and therefore uses its attached Vertex AI service
            # account instead.
            if self.gemini_api_key:
                self._gemini_client = genai.Client(api_key=self.gemini_api_key)
            elif self.project_id:
                self._gemini_client = genai.Client(
                    vertexai=True,
                    project=self.project_id,
                    location=self.location,
                )
        except Exception:
            self._gemini_client = None
        return self._gemini_client

    @staticmethod
    def _parse_model(model, response):
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, model):
            return parsed
        if isinstance(parsed, dict):
            if hasattr(model, "model_validate"):
                return model.model_validate(parsed)
            return model.parse_obj(parsed)
        if hasattr(model, "model_validate_json"):
            return model.model_validate_json(response.text)
        return model.parse_raw(response.text)

    def analyze_complaint(
        self,
        text: str,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> ComplaintAnalysis:
        """Use Gemini structured output, with deterministic local fallback."""
        client = self._get_gemini_client()
        if client is not None:
            try:
                from google.genai import types

                prompt = (
                    "Analyze this citizen report for public-infrastructure planning. "
                    "Classify the underlying issue into exactly one allowed category: "
                    "health, water, sanitation, roads, power, education, or other. "
                    "Estimate civic urgency from 0 to 1, summarize neutrally, name the "
                    "infrastructure need and likely department. Treat an attached image "
                    "only as supporting visual evidence; do not identify people, read "
                    "sensitive data, or claim the image proves when or where it was taken.\n\n"
                    "Citizen report:\n" + text[:6000]
                )
                contents = [prompt]
                if image_bytes and image_mime_type:
                    contents.append(
                        types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type)
                    )
                response = client.models.generate_content(
                    model=self.gemini_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_GeminiComplaintAnalysis,
                        temperature=0.1,
                    ),
                )
                result = self._parse_model(_GeminiComplaintAnalysis, response)
                category = result.category if result.category in scoring.VALID_CATEGORIES else "other"
                return ComplaintAnalysis(
                    category=category,
                    severity_score=result.severity_score,
                    summary=result.summary.strip()[:300],
                    infrastructure_need=result.infrastructure_need.strip()[:200],
                    responsible_department=result.responsible_department.strip()[:160],
                    confidence=result.confidence,
                    image_evidence=(result.image_evidence or "").strip()[:300] or None,
                    provider="gemini",
                )
            except Exception:
                pass

        category = scoring.classify_category(text)
        return ComplaintAnalysis(
            category=category,
            severity_score=scoring.score_severity(text),
            summary=" ".join(text.split())[:300],
            infrastructure_need=category,
            responsible_department=scoring.ESCALATION_TARGETS.get(
                category, scoring.ESCALATION_TARGETS["other"]
            ),
            confidence=0.35,
            image_evidence=None,
            provider="local-fallback",
        )

    def translate_text(self, text: str, source_language: str = "auto") -> Tuple[str, str]:
        """Translate a report to English with Cloud Translation Advanced."""
        if not self.project_id:
            raise RuntimeError("Google Cloud project is not configured")
        from google.cloud import translate_v3 as translate

        client = translate.TranslationServiceClient()
        request = {
            "parent": "projects/{}/locations/global".format(self.project_id),
            "contents": [text],
            "mime_type": "text/plain",
            "target_language_code": "en",
        }
        if source_language and source_language != "auto":
            request["source_language_code"] = source_language.split("-")[0]
        response = client.translate_text(request=request)
        translation = response.translations[0]
        detected = getattr(translation, "detected_language_code", None) or source_language
        return translation.translated_text, detected or "unknown"

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str,
        language_code: str = "hi-IN",
    ) -> Tuple[str, str]:
        """Transcribe short citizen recordings with Cloud Speech-to-Text."""
        if not self.project_id:
            raise RuntimeError("Google Cloud project is not configured")
        from google.cloud import speech

        encoding_map = {
            "audio/webm": speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
            "audio/ogg": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            "audio/opus": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            "audio/mpeg": speech.RecognitionConfig.AudioEncoding.MP3,
            "audio/mp3": speech.RecognitionConfig.AudioEncoding.MP3,
            "audio/flac": speech.RecognitionConfig.AudioEncoding.FLAC,
            "audio/wav": speech.RecognitionConfig.AudioEncoding.LINEAR16,
            "audio/x-wav": speech.RecognitionConfig.AudioEncoding.LINEAR16,
        }
        base_type = (mime_type or "audio/webm").split(";")[0].lower()
        config_args = {
            "language_code": language_code or "hi-IN",
            "enable_automatic_punctuation": True,
            "model": "latest_long",
        }
        encoding = encoding_map.get(base_type)
        if encoding is not None:
            config_args["encoding"] = encoding
        if encoding in {
            speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
            speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        }:
            config_args["sample_rate_hertz"] = 48000

        client = speech.SpeechClient()
        response = client.recognize(
            config=speech.RecognitionConfig(**config_args),
            audio=speech.RecognitionAudio(content=audio_bytes),
        )
        transcript = " ".join(
            result.alternatives[0].transcript
            for result in response.results
            if result.alternatives
        ).strip()
        if not transcript:
            raise RuntimeError("No speech could be recognized")
        detected = next(
            (getattr(result, "language_code", "") for result in response.results if result.alternatives),
            "",
        )
        return transcript, detected or language_code or "unknown"

    def generate_policy_brief(self, context: dict) -> PolicyBrief:
        client = self._get_gemini_client()
        if client is None:
            raise RuntimeError("Gemini is not configured")
        from google.genai import types

        prompt = (
            "Act as a public-infrastructure planning assistant. Recommend one concrete, "
            "bounded project from the supplied evidence. Do not invent budgets, agencies, "
            "statistics, or certainty. Explain how citizen demand, demographics, current "
            "infrastructure and investment context support the recommendation. Include a "
            "practical first action and risks or safeguards. Evidence:\n" + repr(context)[:12000]
        )
        response = client.models.generate_content(
            model=self.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PolicyBrief,
                temperature=0.2,
            ),
        )
        result = self._parse_model(PolicyBrief, response)
        result.provider = "gemini"
        return result

    def geocode(self, query: str) -> Optional[dict]:
        if not self.maps_server_api_key:
            return None
        import requests

        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": self.maps_server_api_key, "region": "in"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "OK" or not payload.get("results"):
            return None
        result = payload["results"][0]
        location = result["geometry"]["location"]
        return {
            "latitude": float(location["lat"]),
            "longitude": float(location["lng"]),
            "region": self._google_region(result.get("address_components", []), query),
            "display_name": result.get("formatted_address", query),
        }

    def reverse_geocode(self, latitude: float, longitude: float) -> Optional[dict]:
        if not self.maps_server_api_key:
            return None
        import requests

        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={
                "latlng": "{},{}".format(latitude, longitude),
                "key": self.maps_server_api_key,
                "result_type": "locality|administrative_area_level_2|administrative_area_level_1",
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "OK" or not payload.get("results"):
            return None
        result = payload["results"][0]
        return {
            "latitude": latitude,
            "longitude": longitude,
            "region": self._google_region(result.get("address_components", []), "Current location"),
            "display_name": result.get("formatted_address", "Current location"),
        }

    @staticmethod
    def _google_region(components: list, fallback: str) -> str:
        preferred = ("locality", "administrative_area_level_2", "administrative_area_level_1")
        for kind in preferred:
            for component in components:
                if kind in component.get("types", []):
                    return component.get("long_name", fallback)
        return fallback

    def capability_status(self) -> dict:
        return {
            "gemini": self.gemini_configured,
            "gemini_mode": self.gemini_mode,
            "speech_to_text": self.cloud_apis_configured,
            "translation": self.cloud_apis_configured,
            "google_maps": bool(self.maps_browser_api_key and self.maps_server_api_key),
            "multimodal": self.gemini_configured,
        }
