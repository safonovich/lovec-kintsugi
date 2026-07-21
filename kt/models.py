"""Общие модели. Лид храним как dict в data/leads.json:
{
  "id": "weevent",            # уникальный слаг
  "name": "WE event",
  "site": "https://weevent.ru",
  "email": null,              # заполняет collect.py или вручную
  "phone": null,              # +7... — используется и для WhatsApp
  "tg": null,                 # @username, если есть
  "segment": "event",         # event = event-агентство (партнёрство/комиссия)
                              # company = прямая компания, HR (тимбилдинг себе)
  "city": "Москва",
  "note": "",                 # заметки: откуда лид, на что упирать
  "status": "new",            # new | offered | sent | skipped | replied
  "sent_ts": null
}
"""
