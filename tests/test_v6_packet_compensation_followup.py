import pytest
from jobhound.v41.compensation import parse_compensation as parse

@pytest.mark.parametrize('text,expected',[
    ('EUR 1.234,56 per hour',{'basis':'unknown','amount_low':None}),
    ('$20/hour CAD',{'basis':'labor_hour','currency':'CAD','amount_low':20}),
    ('USD 20 per hour, paid monthly',{'basis':'labor_hour','amount_low':20}),
    ('USD 500/month or hourly',{'basis':'unknown','amount_low':None}),
    ('USD 20/hr with 3 audio hours each week',{'basis':'labor_hour'}),
    ('USD 20/hr working up to 10 hours',{'basis':'labor_hour','qualifier':None}),
    ('USD 100k - 120000/year',{'basis':'year','amount_low':100000,'amount_high':120000}),
    ('USD 80000 - 100k/year',{'basis':'year','amount_low':80000,'amount_high':100000}),
    ('USD 80k - 100/year',{'basis':'year','amount_low':80000,'amount_high':100000}),
    ('10-20 USD per hour',{'basis':'labor_hour','currency':'USD','amount_low':10,'amount_high':20}),
    ('20 CAD/hour',{'basis':'labor_hour','currency':'CAD','amount_low':20}),
    ('Annual salary: USD 80k',{'basis':'year','amount_low':80000}),
    ('Hourly rate USD 20',{'basis':'labor_hour','amount_low':20}),
    ('Hourly rate USD 500/month',{'basis':'unknown','amount_low':None}),
    ('USD 1e6/year',{'basis':'unknown','amount_low':None}),
    ('USD 20a/hour',{'basis':'unknown','amount_low':None}),
    ('USD 20/hour, CAD accepted',{'basis':'unknown','amount_low':None}),
    ('USD 20 per task-hour',{'basis':'task_hour','labor_hour_equivalent':None}),
    ('USD 20 per accepted image',{'basis':'output_item','labor_hour_equivalent':None}),
    ('USD 20 per recorded hour',{'basis':'output_audio_hour','labor_hour_equivalent':None}),
    ('USD 5/word',{'basis':'output_item','labor_hour_equivalent':None}),
])
def test_followup_contract(text,expected):
    c=parse(text)
    assert {key:getattr(c,key) for key in expected}==expected
