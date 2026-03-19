# Pipeline Layer

- `ingestion.py` parses the source document
- `intents.py` builds paragraph intents and Gemini prompts
- `media.py` owns provider search, selection, downloads, manifests, and run lifecycle seams
- Keep expensive AI work and persistence changes covered by regression tests
