const {test} = require('node:test');
const assert = require('node:assert/strict');
const {create} = require('../../../static/js/assessment_submission.js');
function memory(initial={}) {
  const items = new Map(Object.entries(initial));
  return {get length(){return items.size;}, key:i=>[...items.keys()][i],
    getItem:key=>items.get(key)||null, setItem:(key,value)=>items.set(key,value), removeItem:key=>items.delete(key)};
}
const savedKey = 'livegrade-eval-draft-session-student-1';
const payload = [['turn_id','1'],['submission_key','nonce'],['group_1','80'],['csrfmiddlewaretoken','never-store-this']];
const accepted = {status:'accepted', next_url:'/assess/session/submitted/'};
function response(data, status=200) {return {ok:status>=200&&status<300,status,json:async()=>data};}
function harness(overrides={}) {
  const calls=[], states=[], redirects=[];
  let online=true, token='old', reloads=0;
  const replies=[];
  const options={storage:memory(),session:'session',student:'student',turn:'1',key:'nonce',
    endpoint:'/assess/session/evaluate/',origin:'https://example.test',synchronizeState:true,
    online:()=>online, csrf:()=>token,setCsrf:value=>{token=value;},
    request:async(url,params)=>{calls.push([url,params]);const next=replies.shift();if(next instanceof Error)throw next;return next;},
    onState:(...args)=>states.push(args),redirect:url=>redirects.push(url),reload:()=>{reloads++;},changed:()=>false,...overrides};
  return {options,calls,states,redirects,replies,create:()=>create(options),offline:()=>{online=false;},
    online:()=>{online=true;},get reloads(){return reloads;}};
}
test('confirmed receipt clears only the sent draft and redirects locally', async()=>{
  const h=harness(); const t=h.create();t.saveScores({group_1:'80'});h.replies.push(response(accepted,201));
  await t.submit(payload);assert.equal(t.hasPending,false);assert.equal(h.options.storage.getItem(savedKey),null);
  assert.deepEqual(h.redirects,['/assess/session/submitted/']);assert.equal(h.calls.length,1);
});
test('lost response retains exact payload, refreshes CSRF and retries without changing key', async()=>{
  const h=harness();const t=h.create();t.saveScores({group_1:'80'});h.replies.push(new Error('lost response'));
  await t.submit(payload);assert.equal(t.hasPending,true);assert.deepEqual(t.scores,{group_1:'80'});
  const stored=h.options.storage.getItem(savedKey);assert(!stored.includes('never-store-this'));
  h.replies.push(response({csrf_token:'fresh'}),response({...accepted,replayed:true}));await t.retry();
  assert.equal(h.calls[0][1].body.toString(),h.calls[2][1].body.toString());
  assert.equal(h.calls[2][1].headers['X-CSRFToken'],'fresh');assert.equal(t.hasPending,false);
});
test('offline send is retained and delivered on reconnect using the same turn', async()=>{
  const h=harness();h.offline();const t=h.create();await t.submit(payload);assert.equal(h.calls.length,0);
  assert.equal(t.hasPending,true);h.online();h.replies.push(response({}),response(accepted));await t.synchronize();
  assert.equal(h.calls[1][1].body.get('turn_id'),'1');assert.equal(t.hasPending,false);
});
test('Competition judge scorecard stays tab-scoped and retries every entry without CSRF storage',async()=>{
  const h=harness({storage:memory(),session:'competition-public-id',student:'invitation-id',
    turn:'round-2',endpoint:'/competition/judge/private-token/'});
  const t=h.create();h.offline();
  await t.submit([['entry_11','75.25'],['entry_12','80'],['csrfmiddlewaretoken','private-csrf']]);
  assert.equal(h.calls.length,0);assert.equal(t.hasPending,true);
  const stored=[...Array(h.options.storage.length)].map((_,i)=>h.options.storage.getItem(h.options.storage.key(i))).join('');
  assert(!stored.includes('private-csrf'));assert(stored.includes('75.25'));
  h.online();h.replies.push(response({csrf_token:'rotated'}),
    response({status:'accepted',next_url:'/competition/judge/private-token/'}));
  await t.retry();
  assert.equal(h.calls[1][1].body.get('entry_11'),'75.25');
  assert.equal(h.calls[1][1].body.get('entry_12'),'80');
  assert.equal(h.calls[1][1].body.get('csrfmiddlewaretoken'),null);
  assert.equal(t.hasPending,false);
  assert.deepEqual(h.redirects,['/competition/judge/private-token/']);
});
test('double click while sending does not start another request or alter payload', async()=>{
  const h=harness();let release;h.options.request=async(url,params)=>{h.calls.push([url,params]);return new Promise(resolve=>{release=resolve;});};
  const t=h.create();const first=t.submit(payload);await t.submit([['group_1','999']]);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0][1].body.get('group_1'),'80');release(response(accepted));await first;
});
test('a page reload restores the pending key and scores', async()=>{
  const h=harness();h.offline();const first=h.create();first.saveScores({group_1:'80'});await first.submit(payload);
  h.options.key='a-new-page-key';const restored=h.create();assert.equal(restored.key,'nonce');assert.equal(restored.hasPending,true);
  assert.deepEqual(restored.scores,{group_1:'80'});
});
test('another participant or session never receives this draft', async()=>{
  const h=harness();h.offline();const first=h.create();first.saveScores({group_1:'80'});await first.submit(payload);
  for(const overrides of [{student:'other'},{session:'other'}]) {
    const t=create({...h.options,...overrides});assert.deepEqual(t.scores,{});assert.equal(t.hasPending,false);
  }
});
test('a pending previous turn is replayed to its original turn, not the new form', async()=>{
  const h=harness();h.offline();await h.create().submit(payload);h.options.turn='2';const t=h.create();
  h.online();h.replies.push(response({turn_id:2}),response(accepted));await t.synchronize();
  assert.equal(h.calls[1][1].body.get('turn_id'),'1');assert.equal(h.calls[1][1].body.get('submission_key'),'nonce');
});
test('malformed local draft is ignored safely',()=>{
  for(const value of ['not json','null','[]','{"scores":null}']) {
    const h=harness({storage:memory({[savedKey]:value})});assert.deepEqual(h.create().scores,{});
  }
});
test('unavailable device storage still retains a pending send in this page',async()=>{
  const h=harness({storage:{getItem(){throw Error();},setItem(){throw Error();},removeItem(){throw Error();}}});
  const t=h.create();h.replies.push(new Error());await t.submit(payload);assert.equal(t.hasPending,true);
  h.replies.push(response({}),response(accepted));await t.retry();assert.equal(t.hasPending,false);
});
test('state change on waking reloads the page without resubmitting an unsent draft',async()=>{
  const h=harness({changed:data=>data.turn_id===2});const t=h.create();t.saveScores({group_1:'80'});
  h.replies.push(response({turn_id:2}));await t.synchronize();assert.equal(h.reloads,1);
  assert.equal(h.calls[0][1].method,'GET');assert(h.options.storage.getItem(savedKey));
});
test('a live update confirms a pending send before allowing navigation',async()=>{
  const h=harness();h.offline();const t=h.create();await t.submit(payload);h.online();
  h.replies.push(response({}),response(accepted));await t.requestReload();assert.equal(h.reloads,0);assert.equal(h.redirects.length,1);
});
test('a definitive expired rejection retains scores but ends the unknown send state',async()=>{
  const h=harness();const t=h.create();t.saveScores({group_1:'80'});
  h.replies.push(response({status:'rejected',code:'expired',message:'Time ran out'},410));await t.submit(payload);
  assert.equal(t.hasPending,false);assert.deepEqual(t.scores,{group_1:'80'});
  assert.equal(h.states.at(-1)[0],'rejected');assert(h.options.storage.getItem(savedKey));
});
test('unexpected HTML, server errors and incomplete receipts never erase the pending payload',async()=>{
  for(const reply of [{ok:false,status:500,json:async()=>{throw Error('HTML');}},response({status:'retry'},503),response({})]) {
    const h=harness();const t=h.create();h.replies.push(reply);await t.submit(payload);
    assert.equal(t.hasPending,true);assert(h.options.storage.getItem(savedKey));assert.equal(h.redirects.length,0);
  }
});
test('missing or external confirmation addresses are refused without losing the draft',async()=>{
  for(const url of [undefined,'https://evil.example/','//evil.example/']) {
    const h=harness();const t=h.create();h.replies.push(response({status:'accepted',next_url:url}));await t.submit(payload);
    assert.equal(t.hasPending,true);assert.equal(h.redirects.length,0);assert(h.options.storage.getItem(savedKey));
  }
});
test('expired participant session retains pending work and asks to rejoin',async()=>{
  const h=harness();h.offline();const t=h.create();await t.submit(payload);h.online();
  h.replies.push(response({status:'join_required',message:'Join again'},403));await t.retry();
  assert.equal(t.hasPending,true);assert.equal(h.calls.length,1);assert.equal(h.states.at(-1)[0],'join');
});
test('editing cannot replace an unconfirmed pending submission',async()=>{
  const h=harness();h.offline();const t=h.create();t.saveScores({group_1:'80'});await t.submit(payload);
  t.saveScores({group_1:'99'});assert.deepEqual(t.scores,{group_1:'80'});
});
test('a new authoritative turn clears obsolete unsent drafts only for this participant',()=>{
  const old='livegrade-eval-draft-session-student-0';
  const foreign='livegrade-eval-draft-session-other-0';
  const value=JSON.stringify({scores:{group_1:'80'},key:'old',pending:null});
  const h=harness({storage:memory({[old]:value,[foreign]:value})});h.create();
  assert.equal(h.options.storage.getItem(old),null);assert(h.options.storage.getItem(foreign));
});
test('closed session discards unsent drafts but retains an unconfirmed send',()=>{
  const older='livegrade-eval-draft-session-student-0';
  const pending=JSON.stringify({scores:{group_1:'80'},key:'nonce',pending:payload});
  const unsent=JSON.stringify({scores:{group_1:'10'},key:'unsent',pending:null});
  const h=harness({storage:memory({[savedKey]:pending,[older]:unsent}),discardUnsent:true,turn:''});
  assert.equal(h.create().hasPending,true);assert(h.options.storage.getItem(savedKey));
  assert.equal(h.options.storage.getItem(older),null);
});
