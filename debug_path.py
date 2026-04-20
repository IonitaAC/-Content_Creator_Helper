from pathlib import Path
p = Path("./celery_results").resolve()
print(f"Original: {p}")
print(f"URI: {p.as_uri()}")
