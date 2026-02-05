"""
Google Sheets Sync - синхронізація даних клієнтів з PostgreSQL в Google Sheets
ОПТИМІЗОВАНА ВЕРСІЯ - batch updates замість окремих запитів
"""
import os
import json
import logging
import time
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================================================
# КОНФІГУРАЦІЯ
# ============================================================================

DATABASE_URL = os.getenv('DATABASE_URL')
GOOGLE_SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID')
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Sheet1')
GOOGLE_OAUTH_TOKEN = os.getenv('GOOGLE_OAUTH_TOKEN')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE')

SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', 4 * 60 * 60))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================================
# МАППІНГ КОЛОНОК
# ============================================================================

COLUMNS = {
    'date': 0,           # A
    'full_name': 1,      # B
    'phone': 2,          # C
    'telegram': 3,       # D
    'folder_created': 4, # E
    'passport': 5,       # F
    'ecp': 6,            # G
    'registration': 7,   # H
    'family_income': 8,  # I
    'credit_contracts': 9,   # J
    'debt_certificates': 10, # K
    'expenses': 11,      # L
    'bank_statements': 12,   # M
    'workbook': 13,      # N
    'story': 14,         # O
}

DOC_TYPES = ['passport', 'ecp', 'registration', 'family_income', 'credit_contracts',
             'debt_certificates', 'expenses', 'bank_statements', 'workbook', 'story']

FIRST_DATA_ROW = 2

# ============================================================================
# DATABASE
# ============================================================================

class Database:
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        self.conn.autocommit = True
        logger.info("Database connected")

    def get_all_clients_with_documents(self):
        query = """
            SELECT
                c.id, c.full_name, c.phone, c.telegram_id,
                c.drive_folder_id, c.drive_folder_url, c.created_at,
                ARRAY_AGG(DISTINCT d.document_type) FILTER (WHERE d.document_type IS NOT NULL) as document_types
            FROM docbot.clients c
            LEFT JOIN docbot.documents d ON c.id = d.client_id
            GROUP BY c.id
            ORDER BY c.created_at DESC
        """
        with self.conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()

    def close(self):
        self.conn.close()

# ============================================================================
# GOOGLE SHEETS
# ============================================================================

class SheetsManager:
    def __init__(self):
        if GOOGLE_OAUTH_TOKEN:
            logger.info("Using OAuth 2.0 credentials")
            token_data = json.loads(GOOGLE_OAUTH_TOKEN)
            credentials = Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=token_data.get('token_uri'),
                client_id=token_data.get('client_id'),
                client_secret=token_data.get('client_secret'),
                scopes=['https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/drive']
            )
        elif GOOGLE_CREDENTIALS_FILE:
            logger.info("Using Service Account credentials")
            credentials = service_account.Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=['https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/drive']
            )
        else:
            raise ValueError("No Google credentials!")

        self.service = build('sheets', 'v4', credentials=credentials)
        self.spreadsheet_id = GOOGLE_SPREADSHEET_ID
        self.sheet_name = GOOGLE_SHEET_NAME
        logger.info(f"Sheets API initialized: {GOOGLE_SPREADSHEET_ID}")

    def get_existing_phones(self):
        """Отримати телефони з таблиці"""
        range_name = f"'{self.sheet_name}'!C:C"
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()

        phones = {}
        for i, row in enumerate(result.get('values', [])):
            if row and i >= FIRST_DATA_ROW - 1:
                phone = str(row[0]).strip() if row[0] else ''
                if phone:
                    phones[phone] = i + 1
        return phones

    def ensure_rows(self, needed_rows):
        """Розширити таблицю якщо потрібно"""
        try:
            metadata = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()

            for sheet in metadata.get('sheets', []):
                if sheet['properties']['title'] == self.sheet_name:
                    current = sheet['properties']['gridProperties']['rowCount']
                    if needed_rows > current:
                        to_add = needed_rows - current + 50
                        self.service.spreadsheets().batchUpdate(
                            spreadsheetId=self.spreadsheet_id,
                            body={'requests': [{
                                'appendDimension': {
                                    'sheetId': sheet['properties']['sheetId'],
                                    'dimension': 'ROWS',
                                    'length': to_add
                                }
                            }]}
                        ).execute()
                        logger.info(f"Added {to_add} rows")
                    return
        except Exception as e:
            logger.error(f"Error ensuring rows: {e}")

    def batch_update(self, updates):
        """Виконати batch update"""
        if not updates:
            return
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={'valueInputOption': 'USER_ENTERED', 'data': updates}
        ).execute()

    def _col_letter(self, idx):
        result = ''
        while idx >= 0:
            result = chr(idx % 26 + ord('A')) + result
            idx = idx // 26 - 1
        return result

# ============================================================================
# SYNC
# ============================================================================

def normalize_phone(phone):
    if not phone:
        return ''
    digits = ''.join(filter(str.isdigit, str(phone)))
    if digits.startswith('380'):
        return digits
    if digits.startswith('0'):
        return '38' + digits
    if len(digits) == 10:
        return '380' + digits
    return digits

def sync_to_sheets():
    """Головна функція - BATCH синхронізація"""
    logger.info("=" * 50)
    logger.info("Starting BATCH sync to Google Sheets...")
    start_time = datetime.now()

    try:
        db = Database()
        sheets = SheetsManager()

        # 1. Отримуємо дані
        clients = db.get_all_clients_with_documents()
        logger.info(f"Found {len(clients)} clients in DB")

        existing_phones = sheets.get_existing_phones()
        logger.info(f"Found {len(existing_phones)} in sheet")

        # Нормалізуємо телефони
        normalized_existing = {}
        last_row = FIRST_DATA_ROW - 1
        for phone, row in existing_phones.items():
            norm = normalize_phone(phone)
            if norm:
                normalized_existing[norm] = row
            if row > last_row:
                last_row = row

        logger.info(f"Last row: {last_row}")

        # 2. Готуємо дані для batch update
        all_updates = []
        new_rows = []

        for client in clients:
            phone = client['phone']
            if not phone:
                continue

            phone_norm = normalize_phone(phone)
            doc_types = client.get('document_types') or []
            folder_url = client.get('drive_folder_url') or ''

            existing_row = normalized_existing.get(phone_norm)

            if existing_row:
                # Оновлюємо тільки папку і чекбокси
                # Папка
                all_updates.append({
                    'range': f"'{sheets.sheet_name}'!E{existing_row}",
                    'values': [[folder_url]]
                })
                # Чекбокси (F-O)
                checkboxes = []
                for doc in DOC_TYPES:
                    checkboxes.append(True if doc in doc_types else False)
                all_updates.append({
                    'range': f"'{sheets.sheet_name}'!F{existing_row}:O{existing_row}",
                    'values': [checkboxes]
                })
            else:
                # Новий клієнт - додаємо в список
                created = client['created_at']
                date_str = created.strftime('%d.%m') if created else datetime.now().strftime('%d.%m')
                telegram = f"tg://user?id={client['telegram_id']}" if client.get('telegram_id') else ''

                row_data = [
                    date_str,
                    client['full_name'] or '',
                    phone,
                    telegram,
                    folder_url
                ]
                # Чекбокси
                for doc in DOC_TYPES:
                    row_data.append(True if doc in doc_types else False)

                new_rows.append(row_data)
                normalized_existing[phone_norm] = last_row + len(new_rows)

        # 3. Розширюємо таблицю якщо потрібно
        total_rows_needed = last_row + len(new_rows)
        sheets.ensure_rows(total_rows_needed)

        # 4. Додаємо нові рядки одним запитом
        if new_rows:
            start_row = last_row + 1
            end_row = last_row + len(new_rows)
            sheets.service.spreadsheets().values().update(
                spreadsheetId=sheets.spreadsheet_id,
                range=f"'{sheets.sheet_name}'!A{start_row}:O{end_row}",
                valueInputOption='USER_ENTERED',
                body={'values': new_rows}
            ).execute()
            logger.info(f"Added {len(new_rows)} new clients (rows {start_row}-{end_row})")

        # 5. Оновлюємо існуючі записи batch'ем
        if all_updates:
            # Розбиваємо на чанки по 100 (ліміт API)
            chunk_size = 100
            for i in range(0, len(all_updates), chunk_size):
                chunk = all_updates[i:i+chunk_size]
                sheets.batch_update(chunk)
            logger.info(f"Updated {len(all_updates)//2} existing clients")

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Sync completed in {elapsed:.1f}s: {len(new_rows)} added, {len(all_updates)//2} updated")
        db.close()

    except Exception as e:
        logger.error(f"Sync error: {e}", exc_info=True)
        raise

def run_scheduler():
    logger.info(f"Scheduler: every {SYNC_INTERVAL/3600:.1f} hours")
    while True:
        try:
            sync_to_sheets()
        except Exception as e:
            logger.error(f"Sync failed: {e}")
        logger.info(f"Next sync in {SYNC_INTERVAL/3600:.1f} hours...")
        time.sleep(SYNC_INTERVAL)

if __name__ == '__main__':
    import sys
    if '--once' in sys.argv:
        sync_to_sheets()
    else:
        run_scheduler()
