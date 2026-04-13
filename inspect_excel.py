import pandas as pd
import json

file_path = "HeyPorts_Service_Preloading_Data.xlsx"
try:
    xl = pd.ExcelFile(file_path)
    data = {}
    for sheet_name in xl.sheet_names:
        df = xl.parse(sheet_name)
        data[sheet_name] = df.head(10).to_dict(orient='records')
    
    print(json.dumps({
        "sheet_names": xl.sheet_names,
        "sample_data": data
    }, indent=2, default=str))
except Exception as e:
    print(f"Error: {str(e)}")
