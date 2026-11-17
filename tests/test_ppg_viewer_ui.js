// UI logic regression with an in-memory DOM; does not control any browser.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../tools/ppg_viewer.html'), 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = {};
const drawing = new Proxy({}, {get: () => () => {}});
const element = id => elements[id] ??= {
  value: id === 'span' ? '10' : 'finger', style: {}, textContent: '',
  getBoundingClientRect: () => ({width: 800, height: 205}),
  getContext: () => drawing,
};
const context = vm.createContext({
  document: {getElementById: element},
  window: {devicePixelRatio: 1, addEventListener: () => {}},
  setInterval: () => {},
  setTimeout: () => 1, clearTimeout: () => {}, AbortController,
  // The initial poll remains pending; no network or live capture is started.
  fetch: () => new Promise(() => {}),
});
vm.runInContext(code, context);
const state = {
  connected: true, fresh: true, error: '', estimator_method: 'local_baseline_acdc_reference_curve_v2',
  device_hz: 100, host_hz: 100, window_seconds: 5, metrics: {}, received: 3000,
  seq_gaps: 0, invalid_frames: 0, points: [], port: 'COM6', directory: 'test',
  measurement_remaining: null, measurement_label: null, result: null,
};
function render() { context.input = state; vm.runInContext('render(input)', context); }
render();
assert.match(element('algorithm').textContent, /v2/);
state.result = {label: 'spo2', contact_site_reported: 'finger', requested_duration_s: 30,
  rate_and_delivery_pass: true, device_delivery_hz_per_channel: 100, received_pairs: 3000,
  files: {csv: 'test.csv'}, spo2: {valid: true, spo2_percent: 98.1, valid_windows: 4, total_windows: 5}};
render();
assert.equal(element('spo2value').textContent, '98.1 %');
assert.equal(element('downloads').hidden, false);
state.result = null;
state.measurement_label = 'spo2'; state.measurement_remaining = 20;
render();
assert.equal(element('spo2value').textContent, '—');
assert.equal(element('downloads').hidden, true);
assert.equal(element('spo2button').disabled, true);
state.measurement_label = null; state.measurement_remaining = null;
state.result = {label: 'spo2', contact_site_reported: 'finger', requested_duration_s: 30,
  rate_and_delivery_pass: true, device_delivery_hz_per_channel: 100, received_pairs: 3000,
  spo2: {valid: false, spo2_percent: null, valid_windows: 0, total_windows: 5,
    reasons: ['pulse_below_noise_margin', 'baseline_drift_dominates', 'channel_periods_disagree']}};
render();
assert.equal(element('spo2value').textContent, '—');
assert.match(element('spo2reason').textContent, /脈動相對雜訊不足.*慢速漂移蓋過脈動.*週期不一致/);
state.estimator_method='continuous_acdc_reference_curve_v3';
state.conversion_limits={ratio_min:0.3};
state.result.spo2={valid:false,signal_valid:true,signal_valid_windows:5,conversion_valid_windows:0,
  total_windows:5,signal_ratio_median:0.25,reasons:['outside_demo_curve_range'],warnings:[],
  windows:[{window:1,signal_valid:true,conversion_valid:false,reasons:['outside_demo_curve_range']}]};
render();
assert.match(element('algorithm').textContent,/v3.*0.3/);
assert.match(element('signalquality').textContent,/5／5.*通過/);
assert.match(element('conversionquality').textContent,/0／5/);
assert.match(element('windowdetails').textContent,/波形通過；換算範圍外/);
assert.match(element('spo2quality').textContent,/波形通過/);
assert.equal(element('spo2value').textContent,'—');
state.result=null;state.measurement_phase='stabilizing';state.measurement_label='spo2';
state.stabilization={elapsed_s:7,timeout_s:30,passes:0,required_passes:3,
 quality:{signal_valid:false,signal_reasons:['no_consistent_pulse'],red_periodicity:0.2,periodicity:0.7}};
render();
assert.equal(element('spo2button').disabled,true);
assert.equal(element('start').disabled,true);
assert.match(element('spo2quality').textContent,/等待接觸穩定/);
assert.match(element('spo2reason').textContent,/Red：/);
assert.equal(element('spo2value').textContent,'—');
state.bpm={valid:true,bpm:75.2,age_seconds:0.5,accepted_intervals:10,sample_span_s:10,reasons:[]};
state.result={spo2:{valid:false,signal_valid:false,signal_valid_windows:0,conversion_valid_windows:0,
 total_windows:5,reasons:['outside_demo_curve_range'],windows:[]}};
render();
assert.equal(element('bpmvalue').textContent,'75.2');
assert.equal(element('spo2value').textContent,'—');
state.bpm.valid=false;state.bpm.bpm=null;state.bpm.reasons=['no_consistent_pulse'];render();
assert.equal(element('bpmvalue').textContent,'—');
state.bpm.valid=true;state.bpm.bpm=75.2;state.bpm.age_seconds=3;render();
assert.equal(element('bpmvalue').textContent,'—');
state.bpm.age_seconds=.5;state.fresh=false;render();
assert.equal(element('bpmvalue').textContent,'—');
console.log('PASS: independent BPM, stale clearing, version/domain, separate signal/conversion states, preparation and refusal display');
(async()=>{
  element('bpmvalue').textContent='75.2';
  await vm.runInContext('pending=false; fetch=async()=>{throw Error("test network failure")}; poll()',context);
  assert.equal(element('bpmvalue').textContent,'—');
  assert.match(element('bpmdetail').textContent,/舊 BPM 已清除/);
  assert.equal(vm.runInContext('latest',context),null, 'resize must not redisplay a stale cached BPM');
  console.log('PASS: network failure clears live BPM');
})().catch(error=>{console.error(error);process.exitCode=1;});
