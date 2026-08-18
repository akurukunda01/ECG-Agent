"""OpenRouter-backed judge with a google-generativeai-compatible surface.

The eval scripts were written against the deprecated google.generativeai SDK,
which pins protobuf<6 and cannot coexist with the inference stack's
protobuf>=6 requirement. This adapter keeps the scripts' call sites
(`model.generate_content(prompt).text`) unchanged while routing to the same
underlying judge model (google/gemini-2.5-pro) via OpenRouter's
OpenAI-compatible API.

Environment:
  OPENROUTER_API_KEY  required
  JUDGE_MODEL         optional override, default "google/gemini-2.5-pro"
"""
import os

DEFAULT_JUDGE_MODEL = "google/gemini-2.5-pro"


class _Candidate:
    def __init__(self):
        self.finish_reason = "stop"


class _Response:
    """Mimics the pieces of the genai response the eval scripts touch."""
    def __init__(self, text):
        self.text = text or ""
        self.parts = [self.text] if self.text else []
        self.candidates = [_Candidate()]


class JudgeModel:
    def __init__(self, model_name=None):
        from openai import OpenAI
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
        self.model_name = model_name or os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)

    def generate_content(self, prompt, **_ignored):
        """Drop-in for genai.GenerativeModel.generate_content.

        Extra kwargs (e.g. safety_settings) are accepted and ignored.
        """
        r = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            seed=42,
        )
        return _Response(r.choices[0].message.content)
