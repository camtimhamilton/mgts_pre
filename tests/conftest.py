import os
import tempfile

# Настройки читаются лениво (get_settings), но окружение должно быть готово
# раньше первого запроса: тест не имеет права писать в рабочую базу.
_TMP = tempfile.mkdtemp(prefix="pulse-test-")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["INGEST_KEY"] = ""
os.environ["DEMO_MODE"] = "true"
os.environ["ANALYSIS_MODE"] = "demo"  # Existing tests explicitly cover the preserved legacy demo.
os.environ["LLM_MODE"] = "fake"
