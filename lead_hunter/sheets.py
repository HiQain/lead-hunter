import gspread

from .services import env

DEFAULT_SHEET_ID = "1nGkfNOOdcwTyewgNHlb5RYRKCX3XxsdG_tp-PPHCzFg"


def _worksheet() -> gspread.Worksheet:
    gc = gspread.service_account(filename=env("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"))
    sheet = gc.open_by_key(env("SHEET_ID", DEFAULT_SHEET_ID))
    return sheet.worksheet(env("SHEET_TAB", "Leads_Final"))


def sheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{env('SHEET_ID', DEFAULT_SHEET_ID)}/edit"


def read_rows() -> list[dict]:
    return _worksheet().get_all_records()


def append_leads(leads: list[dict]) -> None:
    """Appends leads, adding any new columns to the header row first (like n8n's auto-map)."""
    if not leads:
        return
    ws = _worksheet()
    header = [h for h in ws.row_values(1) if h]
    new_cols = [k for lead in leads for k in lead if k not in header]
    new_cols = list(dict.fromkeys(new_cols))
    if new_cols:
        header += new_cols
        if len(header) > ws.col_count:
            ws.add_cols(len(header) - ws.col_count)
        ws.update(range_name="A1", values=[header])
    rows = [["" if lead.get(c) is None else str(lead.get(c, "")) for c in header] for lead in leads]
    ws.append_rows(rows, value_input_option="RAW")
