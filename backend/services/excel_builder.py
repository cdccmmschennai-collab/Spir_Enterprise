import pandas as pd
import os

def build_xlsx(rows, spir_no):
    df = pd.DataFrame(rows)

    # ✅ ensure filename has extension
    filename = f"{spir_no or 'output'}.xlsx"

    # ✅ save inside temp folder (or current dir)
    output_path = os.path.join("temp", filename)

    # create folder if not exists
    os.makedirs("temp", exist_ok=True)

    # ✅ specify engine explicitly (IMPORTANT)
    df.to_excel(output_path, index=False, engine="openpyxl")

    return output_path