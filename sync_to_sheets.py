"""
Google Sheets Sync - синхронізація даних клієнтів з PostgreSQL в Google Sheets
Запускається як окремий Background Worker на Render кожні 4 години
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
GOOGLE_SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID')  # ID таблиці з URL
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Таблиця1')  # Назва листа
GOOGLE_OAUTH_TOKEN = os.getenv('GOOGLE_OAUTH_TOKEN')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE')

# Інтервал синхронізації (4 години в секундах)
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', 4 * 60 * 60))

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================================
# МАППІНГ ДОКУМЕНТІВ НА КОЛОНКИ GOOGLE SHEETS
# ============================================================================

# Колонки в Google Sheets (0-indexed) - налаштуй під свою таблицю!
COLUMNS = {
    'date': 0,           # A - Дата
    'full_name': 1,      # B - ПІБ
    'phone': 2,          # C - ТЕЛЕФОН
    'telegram': 3,       # D - Телеграм
    'folder_created': 4, # E - Чи створені папки на диску
    'passport': 5,       # F - паспорт\ІПН
    'ecp': 6,            # G - КЛЮЧ ЕЦП
    'registration': 7,   # H - Довідка про зареєстрованих осіб
    'family_income': 8,  # I - Довідка про доходи
    'credit_contracts': 9,   # J - Кредитні договори
    'debt_certificates': 10, # K - Довідки про заборгованості
    'expenses': 11,      # L - Підвердження витрат
    'bank_statements': 12,   # M - Банківські виписки
    'workbook': 13,      # N - Трудова книжка
    'story': 14,         # O - Історія
}

# Типи документів з БД -> колонки в sheets
DOC_TYPE_TO_COLUMN = {
    'passport': 'passport',
    'ecp': 'ecp',
    'registration': 'registration',
    'family_income': 'family_income',
    'credit_contracts': 'credit_contracts',
    'debt_certificates': 'debt_certificates',
    'expenses': 'expenses',
    'bank_statements': 'bank_statements',
    'workbook': 'workbook',
    'story': 'story',
}

# Перша рядок з даними (після заголовків)
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
        """Отримати всіх клієнтів з їх документами"""
        query = """
            SELECT
                c.id,
                c.full_name,
                c.phone,
                c.telegram_id,
                c.drive_folder_id,
                c.created_at,
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
        # Використовуємо ті ж credentials що і для Drive
        if GOOGLE_OAUTH_TOKEN:
            logger.info("Using OAuth 2.0 credentials for Sheets")
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
            logger.info("Using Service Account credentials for Sheets")
            credentials = service_account.Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=['https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/drive']
            )
        else:
            raise ValueError("No Google credentials configured!")

        self.service = build('sheets', 'v4', credentials=credentials)
        self.spreadsheet_id = GOOGLE_SPREADSHEET_ID
        self.sheet_name = GOOGLE_SHEET_NAME
        logger.info(f"Sheets API initialized for spreadsheet: {GOOGLE_SPREADSHEET_ID}")

    def get_existing_phones(self):
        """Отримати список телефонів які вже є в таблиці"""
        range_name = f"'{self.sheet_name}'!C:C"  # Колонка C - телефони
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()

        values = result.get('values', [])
        phones = {}
        for i, row in enumerate(values):
            if row and i >= FIRST_DATA_ROW - 1:  # Пропускаємо заголовки
                phone = str(row[0]).strip() if row[0] else ''
                if phone:
                    phones[phone] = i + 1  # Номер рядка (1-indexed)
        return phones

    def get_all_data(self):
        """Отримати всі дані з таблиці"""
        range_name = f"'{self.sheet_name}'!A:Z"
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()
        return result.get('values', [])

    def update_checkboxes(self, row_number, document_types, has_folder):
        """Оновити чекбокси для конкретного рядка"""
        updates = []

        # Папка створена
        folder_col = self._col_letter(COLUMNS['folder_created'])
        updates.append({
            'range': f"'{self.sheet_name}'!{folder_col}{row_number}",
            'values': [[True if has_folder else False]]
        })

        # Документи
        for doc_type, col_name in DOC_TYPE_TO_COLUMN.items():
            if col_name in COLUMNS:
                col_letter = self._col_letter(COLUMNS[col_name])
                has_doc = doc_type in (document_types or [])
                updates.append({
                    'range': f"'{self.sheet_name}'!{col_letter}{row_number}",
                    'values': [[True if has_doc else False]]
                })

        # Batch update
        if updates:
            body = {
                'valueInputOption': 'USER_ENTERED',
                'data': updates
            }
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=body
            ).execute()
            logger.info(f"Updated checkboxes for row {row_number}")

    def add_new_client(self, client, last_row):
        """Додати нового клієнта в таблицю"""
        new_row = last_row + 1

        # Формуємо дату
        created_at = client['created_at']
        if created_at:
            date_str = created_at.strftime('%d.%m') if hasattr(created_at, 'strftime') else str(created_at)[:5]
        else:
            date_str = datetime.now().strftime('%d.%m')

        # Telegram username
        telegram = ''
        if client.get('telegram_id'):
            telegram = f"@user{client['telegram_id']}"  # Можна замінити на реальний username якщо зберігається

        # Базові дані (тільки текстові поля, чекбокси окремо)
        row_data = [''] * (max(COLUMNS.values()) + 1)
        row_data[COLUMNS['date']] = date_str
        row_data[COLUMNS['full_name']] = client['full_name'] or ''
        row_data[COLUMNS['phone']] = client['phone'] or ''
        row_data[COLUMNS['telegram']] = telegram

        # Записуємо базові дані
        last_col = self._col_letter(max(COLUMNS.values()))
        range_name = f"'{self.sheet_name}'!A{new_row}:{last_col}{new_row}"
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            body={'values': [row_data]}
        ).execute()

        logger.info(f"Added new client at row {new_row}: {client['full_name']}")

        # Оновлюємо чекбокси
        self.update_checkboxes(
            new_row,
            client.get('document_types', []),
            bool(client.get('drive_folder_id'))
        )

        return new_row

    def _col_letter(self, col_index):
        """Конвертувати індекс колонки в букву (0 -> A, 1 -> B, etc.)"""
        result = ''
        while col_index >= 0:
            result = chr(col_index % 26 + ord('A')) + result
            col_index = col_index // 26 - 1
        return result

# ============================================================================
# SYNC LOGIC
# ============================================================================

def normalize_phone(phone):
    """Нормалізувати телефон для порівняння"""
    if not phone:
        return ''
    # Видаляємо все крім цифр
    digits = ''.join(filter(str.isdigit, str(phone)))
    # Якщо починається з 380 - повертаємо як є
    if digits.startswith('380'):
        return digits
    # Якщо починається з 0 - додаємо 38
    if digits.startswith('0'):
        return '38' + digits
    # Якщо 10 цифр - додаємо 380
    if len(digits) == 10:
        return '380' + digits
    return digits

def sync_to_sheets():
    """Головна функція синхронізації"""
    logger.info("=" * 50)
    logger.info("Starting sync to Google Sheets...")
    logger.info(f"Time: {datetime.now()}")

    try:
        db = Database()
        sheets = SheetsManager()

        # Отримуємо клієнтів з БД
        clients = db.get_all_clients_with_documents()
        logger.info(f"Found {len(clients)} clients in database")

        # Отримуємо існуючі телефони в таблиці
        existing_phones = sheets.get_existing_phones()
        logger.info(f"Found {len(existing_phones)} existing entries in sheet")

        # Створюємо нормалізований маппінг
        normalized_phones = {}
        for phone, row in existing_phones.items():
            normalized = normalize_phone(phone)
            if normalized:
                normalized_phones[normalized] = row

        # Отримуємо всі дані для визначення останнього рядка
        all_data = sheets.get_all_data()
        last_row = len(all_data) if all_data else FIRST_DATA_ROW - 1

        updated = 0
        added = 0

        for client in clients:
            phone = client['phone']
            if not phone:
                continue

            # Нормалізуємо телефон клієнта
            client_phone_normalized = normalize_phone(phone)

            # Шукаємо в існуючих
            found_row = normalized_phones.get(client_phone_normalized)

            if found_row:
                # Оновлюємо чекбокси
                sheets.update_checkboxes(
                    found_row,
                    client.get('document_types', []),
                    bool(client.get('drive_folder_id'))
                )
                updated += 1
            else:
                # Додаємо нового клієнта
                last_row = sheets.add_new_client(client, last_row)
                normalized_phones[client_phone_normalized] = last_row
                added += 1

        logger.info(f"Sync completed: {updated} updated, {added} added")
        db.close()

    except Exception as e:
        logger.error(f"Sync error: {e}", exc_info=True)
        raise

def run_scheduler():
    """Запуск з інтервалом"""
    logger.info(f"Starting scheduler with interval: {SYNC_INTERVAL} seconds ({SYNC_INTERVAL/3600:.1f} hours)")

    while True:
        try:
            sync_to_sheets()
        except Exception as e:
            logger.error(f"Sync failed: {e}")

        logger.info(f"Next sync in {SYNC_INTERVAL/3600:.1f} hours...")
        time.sleep(SYNC_INTERVAL)

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    # Якщо передано аргумент --once - запустити один раз
    import sys
    if '--once' in sys.argv:
        sync_to_sheets()
    else:
        run_scheduler()
