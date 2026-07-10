import os
import re

TEMPLATES_DIR = "templates"

def fix_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    if r"\'" in content:
        content = content.replace(r"\'", "'")
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed {filepath}")

for filename in os.listdir(TEMPLATES_DIR):
    if filename.endswith(".html"):
        fix_file(os.path.join(TEMPLATES_DIR, filename))
print("Done")
