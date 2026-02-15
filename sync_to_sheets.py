"""
Google Sheets Sync - синхронізація даних клієнтів з PostgreSQL в Google Sheets
ОПТИМІЗОВАНА ВЕРСІЯ - batch updates, дедуплікація по телефону
"""
import os
import json
import logging
import time
from datetime import datetime
from collections import defaultdict
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
        """Отримати телефони з таблиці з відстеженням дублікатів"""
        range_name = f"'{self.sheet_name}'!A:O"
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()

        # normalized_phone -> list of row numbers (для виявлення дублікатів)
        phones = defaultdict(list)
        for i, row in enumerate(result.get('values', [])):
            if i < FIRST_DATA_ROW - 1:
                continue
            # Колонка C (індекс 2) - телефон
            if row and len(row) > 2:
                phone = str(row[2]).strip() if row[2] else ''
                if phone:
                    norm = normalize_phone(phone)
                    if norm:
                        phones[norm].append(i + 1)
        return dict(phones)

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
    """Головна функція - BATCH синхронізація з дедуплікацією"""
    logger.info("=" * 50)
    logger.info("Starting BATCH sync to Google Sheets...")
    start_time = datetime.now()

    try:
        db = Database()
        sheets = SheetsManager()

        # 1. Отримуємо дані з БД
        clients = db.get_all_clients_with_documents()
        logger.info(f"Found {len(clients)} clients in DB")

        # 2. Групуємо клієнтів по нормалізованому телефону (об'єднуємо документи)
        clients_by_phone = {}
        for client in clients:
            phone = client['phone']
            if not phone:
                continue
            phone_norm = normalize_phone(phone)
            if not phone_norm:
                continue

            doc_types = set(client.get('document_types') or [])

            if phone_norm not in clients_by_phone:
                clients_by_phone[phone_norm] = {
                    'full_name': client['full_name'] or '',
                    'telegram_id': client.get('telegram_id'),
                    'created_at': client['created_at'],
                    'drive_folder_url': client.get('drive_folder_url') or '',
                    'doc_types': doc_types,
                }
            else:
                # Об'єднуємо документи від різних клієнтів з тим самим телефоном
                clients_by_phone[phone_norm]['doc_types'].update(doc_types)
                # Використовуємо folder_url якщо у поточного його немає
                if not clients_by_phone[phone_norm]['drive_folder_url'] and client.get('drive_folder_url'):
                    clients_by_phone[phone_norm]['drive_folder_url'] = client['drive_folder_url']

        logger.info(f"Unique phones in DB: {len(clients_by_phone)}")

        # 3. Отримуємо телефони з таблиці (з відстеженням дублікатів)
        existing_phones = sheets.get_existing_phones()
        logger.info(f"Found {len(existing_phones)} unique phones in sheet")

        # 4. Обробляємо дублікати: залишаємо перший рядок, очищуємо решту
        normalized_existing = {}
        last_row = FIRST_DATA_ROW - 1
        duplicate_clears = []
        duplicates_found = 0

        for phone_norm, rows in existing_phones.items():
            # Зберігаємо перший рядок як основний
            normalized_existing[phone_norm] = rows[0]
            # Решта - дублікати, очищуємо їх
            for dup_row in rows[1:]:
                duplicate_clears.append({
                    'range': f"'{sheets.sheet_name}'!A{dup_row}:Z{dup_row}",
                    'values': [[''] * 26]
                })
                duplicates_found += 1
            # Відстежуємо максимальний рядок
            for r in rows:
                if r > last_row:
                    last_row = r

        if duplicates_found:
            logger.info(f"Found {duplicates_found} duplicate rows to clear")

        logger.info(f"Last row: {last_row}")

        # 5. Готуємо дані для batch update
        all_updates = []
        new_rows = []

        for phone_norm, data in clients_by_phone.items():
            doc_types = data['doc_types']
            folder_url = data['drive_folder_url']

            existing_row = normalized_existing.get(phone_norm)

            if existing_row:
                # Оновлюємо: ім'я, телефон (нормалізований), папку і чекбокси
                # Телефон перезаписуємо нормалізованим для консистентності
                all_updates.append({
                    'range': f"'{sheets.sheet_name}'!C{existing_row}",
                    'values': [[phone_norm]]
                })
                # Папка
                all_updates.append({
                    'range': f"'{sheets.sheet_name}'!E{existing_row}",
                    'values': [[folder_url]]
                })
                # Чекбокси (F-O)
                checkboxes = [doc in doc_types for doc in DOC_TYPES]
                all_updates.append({
                    'range': f"'{sheets.sheet_name}'!F{existing_row}:O{existing_row}",
                    'values': [checkboxes]
                })
            else:
                # Новий клієнт
                created = data['created_at']
                date_str = created.strftime('%d.%m.%Y') if created else datetime.now().strftime('%d.%m.%Y')
                telegram = f"tg://user?id={data['telegram_id']}" if data.get('telegram_id') else ''

                row_data = [
                    date_str,
                    data['full_name'],
                    phone_norm,
                    telegram,
                    folder_url
                ]
                # Чекбокси
                for doc in DOC_TYPES:
                    row_data.append(doc in doc_types)

                new_rows.append(row_data)
                normalized_existing[phone_norm] = last_row + len(new_rows)

        # 6. Очищуємо дублікати
        if duplicate_clears:
            chunk_size = 100
            for i in range(0, len(duplicate_clears), chunk_size):
                chunk = duplicate_clears[i:i+chunk_size]
                sheets.batch_update(chunk)
            logger.info(f"Cleared {duplicates_found} duplicate rows")

        # 7. Розширюємо таблицю якщо потрібно
        total_rows_needed = last_row + len(new_rows)
        sheets.ensure_rows(total_rows_needed)

        # 8. Додаємо нові рядки одним запитом
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

        # 9. Оновлюємо існуючі записи batch'ем
        if all_updates:
            chunk_size = 100
            for i in range(0, len(all_updates), chunk_size):
                chunk = all_updates[i:i+chunk_size]
                sheets.batch_update(chunk)
            logger.info(f"Updated {len(all_updates)//3} existing clients")

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Sync completed in {elapsed:.1f}s: {len(new_rows)} added, "
                     f"{len(all_updates)//3} updated, {duplicates_found} duplicates cleared")
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
