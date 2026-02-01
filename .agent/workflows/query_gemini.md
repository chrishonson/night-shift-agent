---
description: Query the Gemini model directly via the CLI using a python wrapper.
---

To send a one-off prompt to Gemini:

1. Run the query script
// turbo
python scripts/query_gemini.py "<prompt>" --model <model_name>

**Arguments:**
- `prompt`: The text prompt to send (required).
- `--model`: The Gemini model to use (optional, default: `gemini-1.5-flash`).

**Example:**
`python scripts/query_gemini.py "Explain quantum computing in one sentence" --model gemini-1.5-pro`
