"""Exercise ordinary XHR with an offline page/SDK stand-in, never a network."""
import json
import shutil
import subprocess

import pytest

from app.browser import BrowserError, BrowserRejected, CDPBrowser


PROGRAM = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8')),options=input.options,calls=[];
global.location={origin:options.origin||'https://www.pippit.ai'};
global.window={};
const identity=id=>{window.__businessUserInfoStringify=JSON.stringify({data:{consumer_user:{user_info:{user_id:id}}}})};
identity(options.before||'42');
global.XMLHttpRequest=class{
  open(method,url,async){this.method=method;this.url=url;this.async=async;this.headers={}}
  setRequestHeader(key,value){this.headers[key]=value}
  send(body){calls.push({method:this.method,url:this.url,async:this.async,credentials:this.withCredentials,
    timeout:this.timeout,headers:this.headers,body:JSON.parse(body)});
    if(options.after)identity(options.after);
    if(options.failure==='timeout')return this.ontimeout();
    if(options.failure==='network')return this.onerror();
    this.responseURL=options.responseURL||this.url;this.status=options.status??200;
    this.responseText=JSON.stringify(options.envelope??{ret:'0',data:{is_first_receive:false,
      receive_quota:0,has_popup:true,receive_inter_days:1}});this.onload();}
};
(async()=>process.stdout.write(JSON.stringify({result:await eval(input.expression),calls})))();
"""


@pytest.fixture
def page():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the offline page stand-in')
    browser, reports = CDPBrowser(19301), []
    options = {}
    def evaluate(expression, *, await_promise=False):
        assert await_promise is True and 'fetch(' not in expression
        result = subprocess.run([node, '-e', PROGRAM], input=json.dumps({'expression': expression,
            'options': options}), text=True, capture_output=True, check=True, timeout=5)
        report = json.loads(result.stdout)
        reports.append(report)
        return report['result']
    browser.evaluate = evaluate
    return browser, options, reports


def test_zero_receipt_uses_one_standard_same_origin_xhr_with_configured_timezone(page):
    browser, _, reports = page
    result = browser.daily_checkin_from_page('42', 'Asia/Taipei')
    assert result['receive_quota'] == 0 and result['is_first_receive'] is False
    assert reports[-1]['calls'] == [{'method': 'POST',
        'url': 'https://www.pippit.ai/commerce/v1/benefits/credit_receive',
        'async': True, 'credentials': True, 'timeout': 15000,
        'headers': {'Content-Type': 'application/json'},
        'body': {'source': 'web', 'time_zone': 'Asia/Taipei'}}]


@pytest.mark.parametrize('options', [{'origin': 'https://evilpippit.ai'}, {'before': 'different'}])
def test_wrong_site_or_preclaim_identity_never_sends_a_request(page, options):
    browser, configured, reports = page
    configured.update(options)
    with pytest.raises(BrowserError): browser.daily_checkin_from_page('42', 'Asia/Taipei')
    assert reports[-1]['calls'] == []


@pytest.mark.parametrize('options', [{'after': 'different'}, {'failure': 'timeout'}, {'failure': 'network'},
    {'responseURL': 'https://foreign.test/commerce/v1/benefits/credit_receive'}, {'envelope': {'data': {}}}])
def test_unverified_outcomes_do_not_replay_or_fabricate_a_receipt(page, options):
    browser, configured, reports = page
    configured.update(options)
    with pytest.raises(BrowserError): browser.daily_checkin_from_page('42', 'Asia/Taipei')
    assert len(reports[-1]['calls']) == 1


@pytest.mark.parametrize('options,match', [({'status': 403}, 'HTTP 403'),
    ({'envelope': {'ret': 34070104, 'errmsg': 'shark action check reject', 'data': {}}}, '34070104')])
def test_actual_rejections_remain_errors_without_another_request(page, options, match):
    browser, configured, reports = page
    configured.update(options)
    with pytest.raises(BrowserRejected, match=match): browser.daily_checkin_from_page('42', 'Asia/Taipei')
    assert len(reports[-1]['calls']) == 1
