#!/usr/bin/env python3
"""
Simple script to query Gemini via the CLI tool, matching project conventions.
"""
import subprocess
import sys
import argparse
import json
import os
from dotenv import load_dotenv

# Load .env from project root if possible
# Assuming script is in scripts/, root is one level up
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
load_dotenv(env_path)

def query_gemini(prompt, model="gemini-1.5-flash"):
    cmd = [
        "gemini",
        "--model", model,
        "--output-format", "json",
        prompt
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"Error querying Gemini: {result.stderr}", file=sys.stderr)
            sys.exit(result.returncode)
            
        try:
            output_json = json.loads(result.stdout)
            # Extract text response based on common schemas
            response_text = output_json.get("response") or output_json.get("content") or output_json
            if isinstance(response_text, (dict, list)):
                 print(json.dumps(response_text, indent=2))
            else:
                 print(response_text)
        except json.JSONDecodeError:
            # Fallback for non-JSON output
            print(result.stdout)
            
    except FileNotFoundError:
        print("Error: 'gemini' executable not found in PATH.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query Gemini CLI wrapper")
    parser.add_argument("prompt", help="Text prompt for the model")
    parser.add_argument("--model", default="gemini-1.5-flash", help="Gemini model to use")
    
    args = parser.parse_args()
    query_gemini(args.prompt, args.model)
