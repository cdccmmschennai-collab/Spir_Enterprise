import os
from backend.extraction.spir_extractor import extract_workbook
from openpyxl import load_workbook

TEST_FOLDER = "test_files"


def run_test(file_path):
    print("\n" + "=" * 50)
    print(f"Testing: {os.path.basename(file_path)}")
    print("=" * 50)

    try:
        wb = load_workbook(file_path, data_only=True)
        result = extract_workbook(wb)

        rows = result.get("rows", [])
        tags = [r.get("TAG NO") for r in rows]
        print(f"Rows Extracted: {len(rows)}")
        print("\nUnique Tags:", len(set(tags)))
        print("Total Tags:", len(tags))
        
        if not rows:
            print("WARNING: No data extracted")
            return

        print("\nSample TAGS:")
        for r in rows[:10]:
            print(r.get("TAG NO"))

    except Exception as e:
        print(f"ERROR: {str(e)}")


def run_all():
    if not os.path.exists(TEST_FOLDER):
        print("test_files folder missing")
        return

    files = [f for f in os.listdir(TEST_FOLDER) if f.endswith((".xlsx", ".xls", ".xlsm"))]

    if not files:
        print("No Excel files found in test folder")
        return

    for f in files:
        run_test(os.path.join(TEST_FOLDER, f))


if __name__ == "__main__":
    run_all()

