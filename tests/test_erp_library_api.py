from types import SimpleNamespace
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from erp_api_client import ErpApiClient, ErpApiUnsupported, ErpRpcError


def client_at(monkeypatch):
    page=SimpleNamespace(_erp_runtime_rpc_context={'uid':7,'tz':'Asia/Shanghai','lang':'zh_CN'})
    client=ErpApiClient(page,{'erp_api':{'protocol':'odoo_jsonrpc','enabled_for_write':True,'discovery':{'status':'verified'},'rpc':{'endpoint':'/rpc'}}})
    monkeypatch.setattr(client,'_read_odoo_line_record',lambda key:{'id':1,'name':'sample','cas_code':False,'sequence':1,'reagent_list_id':[2,'SJ001'],'phchproperty_id':[25,'普通类']})
    return client


def library_setup(monkeypatch, result=True, error=None):
    client=client_at(monkeypatch)
    reads=iter([[],[{'id':8,'phchproperty_id':[25,'普通类']} ]])
    def read(model,*args,**kwargs):
        if model=='reagent.list':return [{'id':2,'name':'SJ001','state':'wait'}]
        return next(reads)
    monkeypatch.setattr(client,'_rpc_search_read',read)
    calls=[]
    def rpc(model,method,args,kwargs):
        calls.append((model,method,args,kwargs))
        if error:raise error
        return result
    monkeypatch.setattr(client,'_rpc_call',rpc)
    return client,calls

IDENTITY={'试剂清单号':'SJ001','试剂名称':'sample','CAS号':'','序号':'1'}


def test_library_creation_requires_new_record_with_same_category(monkeypatch):
    client,calls=library_setup(monkeypatch)
    result=client.generate_reagent_library_by_id('1',IDENTITY,'普通类')
    assert result['status']=='created_verified' and result['library_id']==8
    assert calls==[('reagent.list.line','add_reagent_cas',[1],{'context':{'uid':7,'tz':'Asia/Shanghai','lang':'zh_CN'}})]


def test_library_existing_difference_does_not_call_write(monkeypatch):
    client,calls=library_setup(monkeypatch,{'reagent_id':8})
    assert client.generate_reagent_library_by_id('1',IDENTITY,'普通类')['status']=='needs_confirmation'
    assert len(calls)==1 and calls[0][1]=='add_reagent_cas'


@pytest.mark.parametrize('message,exception,status',[
    ('试剂（sample）试剂库已存在，并且没有修改项！','odoo.exceptions.ValidationError','existing_unchanged'),
    ('权限不足','odoo.exceptions.AccessError',None),
    ('其他校验失败','odoo.exceptions.ValidationError',None),
])
def test_library_business_errors_are_not_all_success(monkeypatch,message,exception,status):
    error=ErpRpcError('reagent.list.line','add_reagent_cas',{'data':{'name':exception,'message':message}})
    client,calls=library_setup(monkeypatch,error=error)
    if status:assert client.generate_reagent_library_by_id('1',IDENTITY,'普通类')['status']==status
    else:
        with pytest.raises(ErpRpcError,match=message):client.generate_reagent_library_by_id('1',IDENTITY,'普通类')


@pytest.mark.parametrize('identity,category', [({**IDENTITY,'试剂名称':'other'},'普通类'),({**IDENTITY,'CAS号':'123-45-6'},'普通类'),(IDENTITY,'易燃类')])
def test_library_preflight_blocks_changed_identity_or_category(monkeypatch,identity,category):
    client,calls=library_setup(monkeypatch)
    with pytest.raises(ErpApiUnsupported):client.generate_reagent_library_by_id('1',identity,category)
    assert calls==[]


def test_library_response_without_verified_new_row_is_not_success(monkeypatch):
    client,calls=library_setup(monkeypatch)
    monkeypatch.setattr(client,'_rpc_search_read',lambda model,*a,**kw:[{'id':2,'name':'SJ001','state':'wait'}] if model=='reagent.list' else [])
    assert client.generate_reagent_library_by_id('1',IDENTITY,'普通类')['status']=='unverified'


def test_http_200_rpc_business_error_preserves_reason(monkeypatch):
    client=client_at(monkeypatch)
    monkeypatch.setattr(client,'_request_json',lambda *a,**kw:{'error':{'data':{'name':'odoo.exceptions.ValidationError','message':'具体原因'}}})
    with pytest.raises(ErpRpcError,match='具体原因'):client._rpc_call('reagent.list.line','add_reagent_cas',[1],{})


@pytest.mark.parametrize('existing_changed',[False,True])
def test_match_is_parent_scoped_and_detects_overwrites(monkeypatch,existing_changed):
    client=client_at(monkeypatch)
    before={'id':1,'_erp_parent_id':'2','_erp_parent_state':'wait','phchproperty_id':[51,'易燃类'] if existing_changed else False}
    after={**before,'phchproperty_id':[25,'普通类']}
    reads=iter([[before],[after]])
    monkeypatch.setattr(client,'fetch_reagent_detail',lambda n:next(reads))
    calls=[]
    monkeypatch.setattr(client,'_rpc_call',lambda *args:calls.append(args))
    if existing_changed:
        with pytest.raises(ErpApiUnsupported,match='existing categories'):client.match_reagent_list('SJ001')
    else:assert len(client.match_reagent_list('SJ001')['changes'])==1
    assert calls[0][:3]==('reagent.list','get_phchproperty_nature',[2])
