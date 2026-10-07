"""Source/category accounting in the existing shared HTTP owner's transaction."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib

from .source_policy import SourcePlan

_INSPECTION=ContextVar('jobhound_source_inspection',default=None)

@dataclass(frozen=True)
class SourceInspection:
    run_id: str
    owner_key: str
    source_id: str
    key: str


class SourceBudget:
    def __init__(self,owner,plan):
        self.owner=owner
        # Detach even frozen models: caller-owned nested containers carry no authority.
        self.plan_json=SourcePlan.model_validate(plan).model_dump_json()
        if any(d.observed_at>owner.context.as_of for d in self.plan.policy_decisions):
            raise ValueError('future_source_policy_decision')

    @property
    def plan(self):return SourcePlan.model_validate_json(self.plan_json)

    @property
    def owner_key(self):return hashlib.sha256(str(self.owner.path.resolve()).encode()).hexdigest()

    def initialize(self,db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS source_work(run_id TEXT,source_id TEXT,category TEXT,
                mode TEXT,kind TEXT,key TEXT,state TEXT,holders INTEGER NOT NULL,
                PRIMARY KEY(run_id,source_id,kind,key));
            CREATE TABLE IF NOT EXISTS source_attempts(attempt_id INTEGER PRIMARY KEY,
                run_id TEXT,source_id TEXT,category TEXT,document_key TEXT);
        ''')

    def deferred(self,code,request=None):
        from .bounded_transport import RequestDeferred
        raise RequestDeferred(code,request=request)

    def route(self,source_id):
        route=self.plan.route(source_id)
        if route is None:self.deferred('unknown_source_route')
        if route.mode=='watch':self.deferred('watch_route_audit_only')
        if route.next_check_at is not None and self.owner.context.as_of<route.next_check_at:
            self.deferred('source_cadence_not_due')
        return route

    def key(self,identity):
        from .source_policy import Opaque
        from pydantic import TypeAdapter
        identity=TypeAdapter(Opaque).validate_python(identity)
        return hashlib.sha256(identity.encode()).hexdigest()

    def reserve_work(self,source_id,identity,kind):
        route=self.route(source_id);key=self.key(identity);plan=self.plan
        limit=plan.inspection_ceiling if kind=='inspection' else plan.search_ceiling
        column='inspections' if kind=='inspection' else 'searches'
        with self.owner.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT state FROM source_work WHERE run_id=? AND source_id=? AND kind=? AND key=?',
                (self.owner.context.run_id,source_id,kind,key)).fetchone()
            if prior and prior[0]!='cancelled':
                if kind=='inspection':
                    db.execute('UPDATE source_work SET holders=holders+1 WHERE run_id=? AND source_id=? AND kind=? AND key=?',
                        (self.owner.context.run_id,source_id,kind,key))
                return key
            def used(condition='',values=()):
                return db.execute("SELECT count(*) FROM source_work WHERE run_id=? AND kind=? AND state!='cancelled'"+condition,
                    (self.owner.context.run_id,kind,*values)).fetchone()[0]
            if used()>=limit:self.deferred('source_'+kind+'_ceiling')
            if route.mode=='pilot' and used(" AND mode='pilot'")>=limit//5:
                self.deferred('exploration_'+kind+'_ceiling')
            if used(' AND source_id=?',(source_id,))>=getattr(route,column):
                self.deferred('route_'+kind+'_ceiling')
            if used(' AND category=?',(route.category,))>=getattr(plan.category(route.category),column):
                self.deferred('category_'+kind+'_ceiling')
            state='reserved' if kind=='inspection' else 'inspected'
            db.execute('INSERT INTO source_work VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,source_id,kind,key) DO UPDATE SET state=excluded.state,holders=excluded.holders',
                (self.owner.context.run_id,source_id,route.category,route.mode,kind,key,state,1 if kind=='inspection' else 0))
        return key

    def search(self,source_id,search_id):
        self.reserve_work(source_id,search_id,'search')

    @contextmanager
    def inspection(self,source_id,document_id):
        key=self.reserve_work(source_id,document_id,'inspection')
        scope=SourceInspection(self.owner.context.run_id,self.owner_key,source_id,key)
        token=_INSPECTION.set(scope)
        try:yield scope
        finally:
            _INSPECTION.reset(token)
            # Only a proven unsent/unused reservation is returned. Sent/unknown
            # attempts and explicit cache inspections retain their real cost.
            with self.owner.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE source_work SET holders=max(0,holders-1) WHERE run_id=? AND source_id=? AND kind='inspection' AND key=?",
                    (scope.run_id,scope.source_id,scope.key))
                db.execute("UPDATE source_work SET state='cancelled' WHERE run_id=? AND source_id=? AND kind='inspection' AND key=? AND state='reserved' AND holders=0",
                    (scope.run_id,scope.source_id,scope.key))

    def cached_inspection(self):
        scope=_INSPECTION.get()
        if scope is None or scope.run_id!=self.owner.context.run_id or scope.owner_key!=self.owner_key:
            self.deferred('missing_source_inspection')
        with self.owner.connect() as db:
            db.execute("UPDATE source_work SET state='inspected_cache' WHERE run_id=? AND source_id=? AND kind='inspection' AND key=? AND state='reserved'",
                (scope.run_id,scope.source_id,scope.key))

    def cached_inspection_if_scoped(self):
        if _INSPECTION.get() is None:return False
        self.cached_inspection()
        return True

    def check_http(self,db,request):
        scope=_INSPECTION.get()
        if scope is None:return None  # Existing unclassified adapters retain collection.
        if scope.run_id!=self.owner.context.run_id or scope.owner_key!=self.owner_key:
            self.deferred('source_run_mismatch',request)
        route=self.route(scope.source_id);plan=self.plan
        if request.url.host not in route.hosts:self.deferred('source_host_mismatch',request)
        if (request.url.scheme!='https' or request.url.userinfo
            or any(k.lower() in {'authorization','cookie','x-api-key','x-rapidapi-key'} for k in request.headers)
            or any(k.lower() in {'token','key','api_key','app_key','app_id'} for k in request.url.params)):
            self.deferred('source_route_requires_public_request',request)
        row=db.execute("SELECT state FROM source_work WHERE run_id=? AND source_id=? AND kind='inspection' AND key=?",
            (scope.run_id,scope.source_id,scope.key)).fetchone()
        if row is None or row[0]=='cancelled':self.deferred('source_inspection_not_reserved',request)
        def used(condition,values):
            return db.execute('SELECT count(*) FROM source_attempts WHERE run_id=?'+condition,(scope.run_id,*values)).fetchone()[0]
        if used(' AND source_id=?',(scope.source_id,))>=route.requests:self.deferred('source_request_ceiling',request)
        if used(' AND category=?',(route.category,))>=plan.category(route.category).requests:
            self.deferred('category_request_ceiling',request)
        return scope,route

    def sent(self,db,attempt,checked):
        if checked is None:return
        scope,route=checked
        db.execute('INSERT INTO source_attempts VALUES(?,?,?,?,?)',(attempt,scope.run_id,scope.source_id,route.category,scope.key))
        db.execute("UPDATE source_work SET state='sent_unknown' WHERE run_id=? AND source_id=? AND kind='inspection' AND key=?",
            (scope.run_id,scope.source_id,scope.key))

    def receipt(self,db,used):
        plan=self.plan;run_id=self.owner.context.run_id;rows=[]
        for route in plan.routes:
            states={kind:count for kind,count in db.execute("SELECT kind,count(*) FROM source_work WHERE run_id=? AND source_id=? AND state!='cancelled' GROUP BY kind",(run_id,route.source_id))}
            unknown=db.execute("SELECT count(*) FROM source_work WHERE run_id=? AND source_id=? AND kind='inspection' AND state='reserved'",(run_id,route.source_id)).fetchone()[0]
            requests=db.execute('SELECT count(*) FROM source_attempts WHERE run_id=? AND source_id=?',(run_id,route.source_id)).fetchone()[0]
            rows.append(dict(source_id=route.source_id,category=route.category,mode=route.mode,parser_state=route.parser_state,
                requests_reserved=requests,inspections=states.get('inspection',0)-unknown,
                inspection_reservations=states.get('inspection',0),unknown_unsent_reservations=unknown,
                searches=states.get('search',0)))
        inspections=sum(r['inspections'] for r in rows);searches=sum(r['searches'] for r in rows)
        pilot_inspections=sum(r['inspections'] for r in rows if r['mode']=='pilot')
        return dict(schema='jobhound-source-budget/v1',routes=rows,
            planning_ceilings=dict(requests=plan.request_ceiling,inspections=plan.inspection_ceiling,searches=plan.search_ceiling,
                exploratory_inspections=plan.inspection_ceiling//5,exploratory_searches=plan.search_ceiling//5),
            actual=dict(inspections=inspections,searches=searches,exploratory_inspections=pilot_inspections,
                inspection_reservations=sum(r['inspection_reservations'] for r in rows),
                unknown_unsent_reservations=sum(r['unknown_unsent_reservations'] for r in rows),
                exploration_inspection_share=pilot_inspections/inspections if inspections else None,
                unattributed_existing_requests=used-sum(r['requests_reserved'] for r in rows)),
            limits=['Logical efforts are measured only for explicit source scopes; existing unclassified requests remain visible',
                    'Actual reservations include possible dispatch; failure/throttling is not a refund or zero demand',
                    'Parser admission and adequately covered checks require separate evidence'])
