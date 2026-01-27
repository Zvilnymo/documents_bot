"""
Google Sheets Manager для синхронізації даних з PostgreSQL
Синхронізує клієнтів та статуси документів кожні 4 години
"""
import os
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Конфігурація
GOOGLE_OAUTH_TOKEN = os.getenv('GOOGLE_OAUTH_TOKEN')
GOOGLE_SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID')
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Табличка')

# Типи документів в порядку колонок таблиці
DOCUMENT_COLUMNS = {
    'passport': 4,      # E - Паспорт ІПН
    'ecp': 5,           # F - ЕЦП
    'ecpass': 6,        # G - Пароль ЕЦП
    'registration': 7,  # H - Довідка реєстр
    'workbook': 8,      # I - Трудова книжка
    'credit_contracts': 9,  # J - Кредитні договори
    'bank_statements': 10,  # K - Виписки банк
    'expenses': 11,         # L - Витрати
    'story': 12,            # M - Історія
    'family_income': 13,    # N - Доходи сім'ї
    'debt_certificates': 14, # O - Заборгованості
    'executive': 15,        # P - Виконавчі
    'additional_docs': 16   # Q - Додаткові
}

HEADERS = [
    '№',                    # A
    'ПІБ',                  # B
    'ТЕЛЕФОН',              # C
    'Telegram',             # D
    'Паспорт ІПН',         # E
    'ЕЦП',                 # F
    'Пароль ЕЦП',          # G
    'Довідка реєстр',      # H
    'Трудова книжка',      # I
    'Кредитні договори',   # J
    'Виписки банк',        # K
    'Витрати',             # L
    'Історія',             # M
    'Доходи сім\'ї',       # N
    'Заборгованості',      # O
    'Виконавчі',           # P
    'Додаткові',           # Q
    'Статус',              # R
    'client_id',           # S (прихована)
    'last_sync'            # T (прихована)
]

STATUS_COLUMN = 17  # R - Статус
CLIENT_ID_COLUMN = 18  # S - client_id (прихована)
LAST_SYNC_COLUMN = 19  # T - last_sync (прихована)


class GoogleSheetsManager:
    """Менеджер для синхронізації даних з Google Sheets"""

    def __init__(self):
        """Ініціалізація з OAuth токеном"""
        self.spreadsheet_id = GOOGLE_SPREADSHEET_ID
        self.sheet_name = GOOGLE_SHEET_NAME
        self.service = None
        self._initialize_service()

    def _initialize_service(self):
        """Ініціалізація Google Sheets API сервісу"""
        try:
            if not GOOGLE_OAUTH_TOKEN:
                raise ValueError("GOOGLE_OAUTH_TOKEN not set")
            if not self.spreadsheet_id:
                raise ValueError("GOOGLE_SPREADSHEET_ID not set")

            # Парсимо OAuth токен
            token_data = json.loads(GOOGLE_OAUTH_TOKEN)
            creds = Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=token_data.get('token_uri', 'https://oauth2.googleapis.com/token'),
                client_id=token_data.get('client_id'),
                client_secret=token_data.get('client_secret'),
                scopes=token_data.get('scopes', ['https://www.googleapis.com/auth/spreadsheets'])
            )

            self.service = build('sheets', 'v4', credentials=creds)
            logger.info("Google Sheets service initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets service: {e}")
            raise

    def _safe_execute(self, operation_name: str, operation_func):
        """
        Безпечне виконання операції з Google Sheets
        При помилці логує, але не падає
        """
        try:
            return operation_func()
        except HttpError as e:
            logger.error(f"Google Sheets API error in {operation_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error in {operation_name}: {e}")
            return None

    def _get_all_rows(self) -> List[List[Any]]:
        """Отримати всі рядки з таблиці"""
        def fetch():
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A2:T"  # Від рядка 2 до колонки T
            ).execute()
            return result.get('values', [])

        return self._safe_execute('get_all_rows', fetch) or []

    def _get_existing_client_ids(self) -> Dict[int, int]:
        """
        Отримати словник {client_id: row_number} для існуючих клієнтів
        row_number - це номер рядка в таблиці (починаючи з 2)
        """
        rows = self._get_all_rows()
        client_ids = {}

        for idx, row in enumerate(rows):
            row_number = idx + 2  # +2 бо рахунок з 1 і перший рядок - заголовки
            if len(row) > CLIENT_ID_COLUMN:
                try:
                    client_id = int(row[CLIENT_ID_COLUMN])
                    client_ids[client_id] = row_number
                except (ValueError, IndexError):
                    continue

        return client_ids

    def _get_client_documents(self, db, client_id: int) -> Dict[str, bool]:
        """Отримати статуси документів клієнта з БД"""
        query = """
            SELECT DISTINCT document_type
            FROM docbot.documents
            WHERE client_id = %s
        """
        try:
            result = db.execute(query, (client_id,), fetch=True)
            uploaded_docs = {row['document_type'] for row in result}

            # Перевіряємо чи є пароль від ЕЦП
            query_ecpass = "SELECT password FROM docbot.ec_passwords WHERE client_id = %s"
            ecpass_result = db.execute(query_ecpass, (client_id,), fetch=True)
            if ecpass_result and ecpass_result[0].get('password'):
                uploaded_docs.add('ecpass')

            # Створюємо словник для всіх типів документів
            return {doc_type: (doc_type in uploaded_docs) for doc_type in DOCUMENT_COLUMNS.keys()}

        except Exception as e:
            logger.error(f"Error getting documents for client {client_id}: {e}")
            return {doc_type: False for doc_type in DOCUMENT_COLUMNS.keys()}

    def _prepare_client_row(self, client: Dict, documents: Dict[str, bool], row_number: int) -> List[Any]:
        """
        Підготувати рядок для клієнта
        row_number - порядковий номер в таблиці
        """
        # Базові дані
        row = [
            row_number,                          # A - №
            client.get('full_name', ''),        # B - ПІБ
            client.get('phone', ''),            # C - ТЕЛЕФОН
            str(client.get('telegram_id', '')), # D - Telegram
        ]

        # Чекбокси документів (E-Q)
        for doc_type in ['passport', 'ecp', 'ecpass', 'registration', 'workbook',
                         'credit_contracts', 'bank_statements', 'expenses', 'story',
                         'family_income', 'debt_certificates', 'executive', 'additional_docs']:
            row.append('✅' if documents.get(doc_type, False) else '❌')

        # Статус
        status_text = 'Завершено' if client.get('status') == 'completed' else 'В процесі'
        row.append(status_text)  # R - Статус

        # Приховані колонки
        row.append(client.get('id'))  # S - client_id
        row.append(datetime.now().isoformat())  # T - last_sync

        return row

    def _batch_update_rows(self, updates: List[Dict]) -> bool:
        """
        Пакетне оновлення рядків
        updates: [{'range': 'A2:T2', 'values': [[...]]}, ...]
        """
        if not updates:
            return True

        def update():
            body = {
                'valueInputOption': 'USER_ENTERED',
                'data': updates
            }
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=body
            ).execute()
            return True

        result = self._safe_execute('batch_update_rows', update)
        return result is not None

    def _append_rows(self, rows: List[List[Any]]) -> bool:
        """Додати нові рядки в кінець таблиці"""
        if not rows:
            return True

        def append():
            body = {'values': rows}
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A:T",
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            return True

        result = self._safe_execute('append_rows', append)
        return result is not None

    def initial_sync(self, db):
        """
        Початкова синхронізація - вивантажити ВСІ дані з PostgreSQL
        Викликається ОДИН РАЗ при першому запуску
        """
        logger.info("Starting initial sync...")

        try:
            # Отримуємо всіх клієнтів з БД
            query = "SELECT * FROM docbot.clients ORDER BY id"
            clients = db.execute(query, fetch=True)

            if not clients:
                logger.info("No clients found in database")
                return True

            # Перевіряємо чи таблиця порожня (окрім заголовків)
            existing_rows = self._get_all_rows()
            if len(existing_rows) > 0:
                logger.warning("Table is not empty! Use incremental_sync instead")
                return False

            # Додаємо заголовки якщо їх немає
            first_row = self._safe_execute('get_first_row',
                lambda: self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{self.sheet_name}!A1:T1"
                ).execute().get('values', []))

            if not first_row or len(first_row[0]) < len(HEADERS):
                header_update = [{
                    'range': f"{self.sheet_name}!A1:T1",
                    'values': [HEADERS]
                }]
                self._batch_update_rows(header_update)
                logger.info("Headers added to spreadsheet")

            # Готуємо всі рядки батчами по 100
            all_rows = []
            batch_size = 100

            for idx, client in enumerate(clients, start=1):
                documents = self._get_client_documents(db, client['id'])
                row = self._prepare_client_row(client, documents, idx)
                all_rows.append(row)

                # Додаємо батч коли накопичилося 100 рядків
                if len(all_rows) >= batch_size:
                    self._append_rows(all_rows)
                    logger.info(f"Synced {len(all_rows)} clients...")
                    all_rows = []

            # Додаємо останні рядки
            if all_rows:
                self._append_rows(all_rows)

            logger.info(f"Initial sync completed: {len(clients)} clients synced")
            return True

        except Exception as e:
            logger.error(f"Error during initial sync: {e}")
            return False

    def incremental_sync(self, db):
        """
        Інкрементальна синхронізація - оновлює ТІЛЬКИ:
        1. Додає нових клієнтів
        2. Оновлює чекбокси документів для існуючих
        3. Оновлює статус клієнта
        НЕ чіпає кольори, коментарі та інші ручні зміни
        """
        logger.info("Starting incremental sync...")

        try:
            # Отримуємо існуючих клієнтів з таблиці
            existing_clients = self._get_existing_client_ids()
            logger.info(f"Found {len(existing_clients)} existing clients in spreadsheet")

            # Отримуємо всіх клієнтів з БД
            query = "SELECT * FROM docbot.clients ORDER BY id"
            db_clients = db.execute(query, fetch=True)

            if not db_clients:
                logger.info("No clients found in database")
                return True

            new_clients = []
            updates = []

            for client in db_clients:
                client_id = client['id']
                documents = self._get_client_documents(db, client_id)

                if client_id in existing_clients:
                    # Клієнт вже є - оновлюємо ТІЛЬКИ документи + статус
                    row_number = existing_clients[client_id]

                    # Готуємо оновлення для чекбоксів (колонки E-Q) + статус (R) + last_sync (T)
                    doc_values = []
                    for doc_type in ['passport', 'ecp', 'ecpass', 'registration', 'workbook',
                                     'credit_contracts', 'bank_statements', 'expenses', 'story',
                                     'family_income', 'debt_certificates', 'executive', 'additional_docs']:
                        doc_values.append('✅' if documents.get(doc_type, False) else '❌')

                    status_text = 'Завершено' if client.get('status') == 'completed' else 'В процесі'

                    # Оновлюємо документи (E-Q), статус (R) і last_sync (T)
                    updates.append({
                        'range': f"{self.sheet_name}!E{row_number}:Q{row_number}",
                        'values': [doc_values]
                    })
                    updates.append({
                        'range': f"{self.sheet_name}!R{row_number}",
                        'values': [[status_text]]
                    })
                    updates.append({
                        'range': f"{self.sheet_name}!T{row_number}",
                        'values': [[datetime.now().isoformat()]]
                    })

                else:
                    # Новий клієнт - додаємо повний рядок
                    next_row_num = len(existing_clients) + len(new_clients) + 2
                    row = self._prepare_client_row(client, documents, next_row_num - 1)
                    new_clients.append(row)

            # Оновлюємо існуючих клієнтів батчами по 100
            if updates:
                for i in range(0, len(updates), 100):
                    batch = updates[i:i+100]
                    self._batch_update_rows(batch)
                    logger.info(f"Updated {len(batch)//3} existing clients...")

            # Додаємо нових клієнтів батчами по 100
            if new_clients:
                for i in range(0, len(new_clients), 100):
                    batch = new_clients[i:i+100]
                    self._append_rows(batch)
                    logger.info(f"Added {len(batch)} new clients...")

            logger.info(f"Incremental sync completed: {len(new_clients)} new, {len(updates)//3} updated")
            return True

        except Exception as e:
            logger.error(f"Error during incremental sync: {e}")
            return False

    def update_client_documents_realtime(self, db, client_id: int):
        """
        Оновити документи конкретного клієнта в реальному часі
        Викликається після завантаження документа
        """
        try:
            existing_clients = self._get_existing_client_ids()

            if client_id not in existing_clients:
                logger.warning(f"Client {client_id} not found in spreadsheet")
                return False

            row_number = existing_clients[client_id]
            documents = self._get_client_documents(db, client_id)

            # Готуємо оновлення документів
            doc_values = []
            for doc_type in ['passport', 'ecp', 'ecpass', 'registration', 'workbook',
                             'credit_contracts', 'bank_statements', 'expenses', 'story',
                             'family_income', 'debt_certificates', 'executive', 'additional_docs']:
                doc_values.append('✅' if documents.get(doc_type, False) else '❌')

            # Оновлюємо тільки колонки документів
            updates = [{
                'range': f"{self.sheet_name}!E{row_number}:Q{row_number}",
                'values': [doc_values]
            }]

            return self._batch_update_rows(updates)

        except Exception as e:
            logger.error(f"Error updating client documents in real-time: {e}")
            return False


# Глобальний instance
sheets_manager = None

def get_sheets_manager():
    """Отримати глобальний instance менеджера"""
    global sheets_manager
    if sheets_manager is None:
        try:
            sheets_manager = GoogleSheetsManager()
        except Exception as e:
            logger.error(f"Failed to create GoogleSheetsManager: {e}")
            sheets_manager = None
    return sheets_manager
