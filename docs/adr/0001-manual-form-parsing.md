# ADR 0001. Ручной разбор форм без python-multipart

## Контекст

Формы системы (регистрация акта ОТК, правка весов индекса) отправляются методом
POST с `Content-Type: application/x-www-form-urlencoded`. FastAPI умеет
разбирать формы, но для этого требует стороннюю зависимость
`python-multipart`. Контракт (`docs/contract.md` §1 и §5.1) фиксирует, что
`python-multipart` в `requirements.txt` отсутствует намеренно, а тело формы
разбирается вручную через `urllib.parse.parse_qsl`.

## Решение

* Тело запроса буферизуется middleware `buffer_form_body` в `app/main.py`:
  для методов `POST`, `PUT`, `PATCH` оно сохраняется в
  `request.state.raw_body = await request.body()`. Это нужно потому, что
  обработчики синхронные и не могут повторно прочитать асинхронный поток.
* Разбор выполняет `read_form()` в `app/web/context.py`. Он декодирует
  `request.state.raw_body` как UTF-8 (с `errors="replace"`) и вызывает
  `urllib.parse.parse_qsl(text, keep_blank_values=True, max_num_fields=2000)`.
  Пустые значения сохраняются, число полей ограничено сверху.
* Результат — обычный `dict[str, str]`; при повторе ключа побеждает последнее
  значение.
* `app/config.py` фиксирует `FORM_CSP = "application/x-www-form-urlencoded"`;
  `python-multipart` отсутствует в `requirements.txt`.

## Последствия

Положительные:

* На одну зависимость меньше; меньше поверхность обновлений.
* Полный контроль над форматом ошибок и над разбором повторяющихся ключей.

Отрицательные:

* `multipart/form-data` (загрузка файлов) не поддерживается — в системе она и
  не нужна.
* Тело любого `POST`/`PUT`/`PATCH` читается в память middleware целиком;
  стриминговая обработка тела недоступна.

## Статус

Принято.
