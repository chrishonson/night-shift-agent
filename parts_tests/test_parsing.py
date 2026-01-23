import re
import os
from unittest.mock import MagicMock
# Context constants
MAX_TOOL_OUTPUT_CHARS = 1000
class ToolboxMock:
    def read_file(self, path):
        return f"Content of {path}"
    def _parse_and_contextualize_errors(self, output: str) -> str:
        """Parses compiler errors, reads source files, and returns a focused error report."""
        # Regex patterns for common Kotlin/Gradle errors
        # 1. e: file:///path/to/File.kt:10:20: error: message
        # 2. e: /path/to/File.kt: (10, 20): message
        error_patterns = [
            r"(?P<level>[ew]):\s+(?P<path>file://[^:]+|/[^:]+):\s*\(?(?P<line>\d+)[:,\s]+(?P<col>\d+)\)?:\s*(?P<msg>.*)",
            r"(?P<path>[^:\n]+\.kt):\s*(?P<line>\d+):\s*(?P<col>\d+):\s*error:\s*(?P<msg>.*)"
        ]
        
        extracted_errors = []
        files_to_read = set()
        
        for line in output.split('\n'):
            for pattern in error_patterns:
                match = re.search(pattern, line)
                if match:
                    path_str = match.group("path").strip()
                    # Fix file:// prefix
                    if path_str.startswith("file://"):
                        path_str = path_str[7:]
                    
                    # Normalize absolute paths
                    # MOCKING os.path.exists for test
                    files_to_read.add(path_str)
                    
                    extracted_errors.append(line)
                    break # Single match per line
                    
        if not extracted_errors:
             if len(output) > MAX_TOOL_OUTPUT_CHARS:
                half = MAX_TOOL_OUTPUT_CHARS // 2
                return output[:half] + "\n\n... [TRUNCATED] ...\n\n" + output[-half:]
             return output
        # Read files
        file_contexts = []
        for file_path in files_to_read:
             try:
                 # MOCKING file read
                 content = f"fake content of {file_path}"
                 file_contexts.append(f"--- FILE: {file_path} ---\n{content}\n")
             except Exception as e:
                 file_contexts.append(f"--- FILE: {file_path} ---\n[Error reading file: {e}]\n")
        
        # Assemble report
        report_parts = [
            "❌ BUILD FAILED - ERROR REPORT",
            "\n=== EXTRACTED ERRORS ==="
        ]
        report_parts.extend(extracted_errors)
        
        report_parts.append("\n=== SOURCE FILES CONTEXT ===")
        report_parts.extend(file_contexts)
        
        report_parts.append("\n=== RAW OUTPUT TAIL (Last 2000 chars) ===")
        report_parts.append(output[-2000:])
        
        return "\n".join(report_parts)
def test():
    t = ToolboxMock()
    
    # Case 1: Standard Kotlin URI format
    output1 = """
    > Task :compileKotlin FAILED
    e: file:///Users/nick/Project/File1.kt:10:20: error: Unresolved reference: foo
    Some other gradle output
    """
    res1 = t._parse_and_contextualize_errors(output1)
    print("--- Test 1 ---")
    print(res1)
    assert "Unresolved reference: foo" in res1
    assert "fake content of /Users/nick/Project/File1.kt" in res1
    
    # Case 2: Parentheses extraction style
    output2 = """
    e: /Users/nick/Project/File2.kt: (5, 10): Val cannot be reassigned
    """
    res2 = t._parse_and_contextualize_errors(output2)
    print("\n--- Test 2 ---")
    print(res2)
    assert "Val cannot be reassigned" in res2
    assert "fake content of /Users/nick/Project/File2.kt" in res2
    
    # Case 3: No match (Fallback)
    output3 = "BUILD FAILED due to unknown reason"
    res3 = t._parse_and_contextualize_errors(output3)
    print("\n--- Test 3 ---")
    print(res3)
    assert res3 == output3
if __name__ == "__main__":
    test()