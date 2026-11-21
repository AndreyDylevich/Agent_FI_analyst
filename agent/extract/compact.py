"""Компактный контракт: даты/статьи задаёт план, ИИ выбирает исходные значения и адреса строк."""

import json

from agent.extract.schemas import CITATION, _keys, _object, _unique_object
from agent.metrics.data import UNIT_MULTIPLIERS

VERSION=1
INSTRUCTION='''
В этом запросе действует КОМПАКТНЫЙ контракт. Верни только number_format, unit, unit_evidence
и observations по схеме. Название компании, стандарт, охват и период уже подтверждены Python:
не переписывай их. В observations ключ каждой запрошенной пары статья/дата задан схемой.
Для каждого ключа выбери исходную подпись, исходное число, адрес строк evidence и cash_flow_section.
Не вычисляй даты и не добавляй item_id/period_start/period_end/column_header/statement в ответ.
Отсутствующий или неоднозначный факт — JSON null под его ключом, никогда не ноль и не прочерк.
Глобальная unit относится ко всем выбранным суммам; строку с другим масштабом/валютой пропусти.
Для датированных остатков капитала используй только dated_row_hints нужной пары и колонку
владельца, заданную суффиксом total/parent/nci. Прибыль за период не равна совокупному доходу.
Если вместо одного раскрытого итога есть несколько составляющих, не суммируй их: верни null.'''


def observation_key(row: dict) -> str:
    return row['item_id']+'__'+row['period_end'].replace('-','_')


def schema(plan: list[dict], numbers: list[int], flow_items: set[str] | None = None) -> dict:
    ref=_object({**CITATION['properties'],'page':{'type':'integer','enum':numbers}})
    found=_object({'original_name':{'type':'string'},'raw_value':{'type':'string','pattern':r'^[-+−(]?[0-9][0-9 ,.()]*$'},
                   'evidence':ref,'cash_flow_section':{'type':['string','null'],
                     'enum':[None,'operating','investing','financing','mixed']}})
    found={**found,'type':['object','null']}
    observations={}
    for row in plan:
        row_schema=found
        if flow_items is not None and row['item_id'] not in flow_items:
            row_schema={**found,'properties':{**found['properties'],'cash_flow_section':{'type':'null'}}}
        observations[observation_key(row)]=row_schema
    return _object({'number_format':{'type':'string','enum':['space_grouped_decimal','comma_grouped_dot_decimal']},
                    'unit':{'type':'string','enum':list(UNIT_MULTIPLIERS)},'unit_evidence':ref,
                    'observations':_object(observations)})


def expand(content: str, *, plan: list[dict], context: dict, statement: str) -> str:
    """Привязывает извлечённое к запрошенной паре. Не меняет и не складывает финансовые числа."""
    data=json.loads(content,object_pairs_hook=_unique_object)
    _keys(data,{'number_format','unit','unit_evidence','observations'})
    wanted={observation_key(row):row for row in plan}
    _keys(data['observations'],set(wanted))
    if not context:raise ValueError('Для компактного ответа нужен подтверждённый контекст PDF')
    facts=[];warnings=[]
    for key,row in wanted.items():
        found=data['observations'][key]
        if found is None:
            warnings.append(f'{key}: факт пропущен моделью; отсутствие не равно нулю');continue
        _keys(found,{'original_name','raw_value','evidence','cash_flow_section'})
        facts.append({key:row[key] for key in ('item_id','period_start','period_end','period_type','column_header')} |
            {**found,'number_format':data['number_format'],'unit':data['unit'],
             'unit_evidence':data['unit_evidence'],'statement':statement})
    return json.dumps({**context,'facts':facts,'warnings':warnings},ensure_ascii=False)
