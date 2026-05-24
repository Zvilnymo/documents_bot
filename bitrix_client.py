import logging
import requests

logger = logging.getLogger(__name__)

# Smart Process entity type ID for invoices in Bitrix24 (new SPA invoices)
INVOICE_ENTITY_TYPE_ID = 31


class BitrixClient:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.rstrip('/')

    def _call(self, method: str, params: dict = None):
        url = f"{self.webhook_url}/{method}.json"
        try:
            response = requests.post(url, json=params or {}, timeout=30)
            data = response.json()
            if 'error' in data:
                logger.error(f"Bitrix API error [{method}]: {data.get('error')} — {data.get('error_description', '')}")
                return None
            return data.get('result')
        except Exception as e:
            logger.error(f"Bitrix API call failed [{method}]: {e}")
            return None

    def find_contact_by_phone(self, phone: str):
        """Find Bitrix24 contact by phone. Tries multiple formats."""
        digits = ''.join(c for c in phone if c.isdigit())
        for query in [phone, f'+{digits}', digits]:
            result = self._call('crm.contact.list', {
                'filter': {'PHONE': query},
                'select': ['ID', 'NAME', 'LAST_NAME'],
            })
            if result:
                return result[0]
        return None

    def get_invoices_by_contact(self, contact_id) -> list:
        """Get SPA invoices (entityTypeId=31) linked to a contact."""
        result = self._call('crm.item.list', {
            'entityTypeId': INVOICE_ENTITY_TYPE_ID,
            'filter': {'contactId': int(contact_id)},
            'select': ['id', 'title', 'opportunity', 'currencyId',
                       'stageId', 'createdTime', 'closedate', 'accountNumber'],
            'order': {'createdTime': 'DESC'},
        })
        if isinstance(result, dict) and 'items' in result:
            return result['items']
        return []

    def get_invoice(self, invoice_id):
        """Get a single SPA invoice by ID."""
        result = self._call('crm.item.get', {
            'entityTypeId': INVOICE_ENTITY_TYPE_ID,
            'id': int(invoice_id),
        })
        if isinstance(result, dict) and 'item' in result:
            return result['item']
        return result  # may already be the item dict

    def add_timeline_comment(self, invoice_id, comment: str):
        """Add a comment to the invoice timeline (SPA entity)."""
        return self._call('crm.timeline.comment.add', {
            'fields': {
                'ENTITY_ID': int(invoice_id),
                'ENTITY_TYPE': f'crm_{INVOICE_ENTITY_TYPE_ID}',
                'COMMENT': comment,
            }
        })

    def notify_manager(self, manager_bitrix_id, message: str):
        """Send a personal notification to a Bitrix24 user."""
        return self._call('im.notify.personal.add', {
            'USER_ID': str(manager_bitrix_id),
            'MESSAGE': message,
            'TYPE': 'NOTIFY',
        })

    # ── Known stage map for this Bitrix installation (entityTypeId=31) ─────────
    # Fetched via crm.status.list and decoded from UTF-8 bytes.
    # SEMANTICS: S = success (paid), F = failure (canceled), None = in-progress
    _STAGE_MAP = {
        'DT31_1:N':          ('🔵 Чорновик',              False, False),
        'DT31_1:UC_842ODN':  ('📤 Погодили на відправку', False, False),
        'DT31_1:S':          ('📨 Відправлений клієнту',  False, False),
        'DT31_1:UC_OH8Y4S':  ('⚠️ Прострочений',          False, False),
        'DT31_1:UC_H5PPNK':  ('⏸ Пауза',                  False, False),
        'DT31_1:UC_WW75SB':  ('✅ Оплатили',               True,  False),
        'DT31_1:P':          ('✅ Оплатили',               True,  False),
        'DT31_1:UC_FKX3CW':  ('❌ Відмова',                False, True),
        'DT31_1:D':          ('❌ Скасували',              False, True),
    }

    @classmethod
    def get_stage_name(cls, stage_id: str) -> str:
        return cls._STAGE_MAP.get(stage_id, (f'❓ {stage_id}', False, False))[0]

    @classmethod
    def is_paid_stage(cls, stage_id: str) -> bool:
        return cls._STAGE_MAP.get(stage_id, ('', False, False))[1]

    @classmethod
    def is_rejected_stage(cls, stage_id: str) -> bool:
        return cls._STAGE_MAP.get(stage_id, ('', False, False))[2]

    @staticmethod
    def format_amount(price, currency: str = 'UAH') -> str:
        try:
            return f"{float(price):,.2f} {currency}".replace(',', ' ')
        except Exception:
            return f"{price} {currency}"
