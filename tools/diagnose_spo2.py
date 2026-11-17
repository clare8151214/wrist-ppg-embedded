"""Offline investigation of frozen captures; no medical output or live changes.

Run using a Python with numpy/matplotlib. Reference runner is diagnostic only.
"""
from pathlib import Path
import csv
import json
import math
from statistics import mean, pstdev
import subprocess
import sys
import importlib.util

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'logs/spo2-diagnosis-20261117'
FROZEN = ROOT / 'logs/spo2-20261117/source/tools/spo2_estimator.py'
spec = importlib.util.spec_from_file_location('frozen_spo2_v1', FROZEN)
v1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v1)
smooth, detrend, periodicity, window_estimate = v1.smooth, v1.detrend, v1.periodicity, v1.window_estimate


def ablation(all_points):
    source = FROZEN.read_text(encoding='utf-8')
    gate = '    if min(red_pi, ir_pi) < 0.0005:\n        reasons.append("pulse_too_weak")\n'
    assert source.count(gate) == 1
    namespace = {'__name__': 'diagnostic_ablation_only'}
    exec(compile(source.replace(gate, ''), '<diagnostic_no_pi_gate>', 'exec'), namespace)
    trials = []
    for trial, points in enumerate(all_points, 1):
        original = v1.estimate_spo2(points)
        probe = namespace['estimate_spo2'](points)
        trials.append(dict(trial=trial, original_valid_windows=original['valid_windows'],
                           without_fixed_pi_gate_valid_windows=probe['valid_windows'],
                           without_fixed_pi_gate_window_reasons=[w['reasons'] for w in probe['windows']],
                           original_final_output_available=original['valid'],
                           diagnostic_final_output_available=probe['valid']))
    controls = []
    for scale in (1, .4):
        points = []
        for i in range(3001):
            t=i/100; pulse=math.sin(2*math.pi*1.25*t)
            points.append((1,i,round(t*1e6),round(100000+120*scale*pulse),
                           round(120000+240*scale*pulse),100+t))
        result=v1.estimate_spo2(points)
        controls.append(dict(no_added_noise=True,amplitude_scale=scale,true_ratio=.6,
                             pulse_frequency_hz=1.25,valid_windows=result['valid_windows'],
                             reasons=result['reasons'],
                             correlations=[w.get('red_ir_correlation') for w in result['windows']]))
    report=dict(scope='DIAGNOSTIC ONLY; production code and live thresholds unchanged; no SpO2 numbers from ablation are displayed or accepted',trials=trials,noise_free_controls=controls)
    (OUT/'ablation.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


def debug_window(points):
    groups = [points[i:i+4] for i in range(0, len(points)-3, 4)]
    red = smooth([mean(p[3] for p in g) for g in groups])
    ir = smooth([mean(p[4] for p in g) for g in groups])
    ts = [mean(p[2] for p in g) / 1e6 for g in groups]
    fs = (len(ts)-1)/(ts[-1]-ts[0])
    ir_ac = detrend(ir)
    lag, score = periodicity(ir_ac, fs)
    result = dict(lag_samples=lag, fs=fs, periodicity=score, troughs=[], beats=[])
    if lag is None:
        return result
    candidates = [i for i in range(1,len(ir_ac)-1) if ir_ac[i] <= -.2*pstdev(ir_ac)
                  and ir_ac[i] < ir_ac[i-1] and ir_ac[i] <= ir_ac[i+1]]
    troughs=[]
    for i in sorted(candidates,key=lambda i:ir_ac[i]):
        if all(abs(i-j)>=.6*lag for j in troughs):
            troughs.append(i)
    troughs.sort();result['troughs']=troughs
    for a,b in zip(troughs,troughs[1:]):
        if not .65*lag<=b-a<=1.45*lag:
            result['beats'].append(dict(a=a,b=b,reject='interval'))
            continue
        ip=max(range(a+1,b),key=lambda i:ir[i])
        rp=max(range(a+1,b),key=lambda i:red[i])
        baseline_red=lambda i:red[a]+(red[b]-red[a])*(i-a)/(b-a)
        baseline_ir=lambda i:ir[a]+(ir[b]-ir[a])*(i-a)/(b-a)
        rp_corrected=max(range(a+1,b),key=lambda i:red[i]-baseline_red(i))
        ip_corrected=max(range(a+1,b),key=lambda i:ir[i]-baseline_ir(i))
        result['beats'].append(dict(a=a,b=b,raw_peak_delay_s=abs(rp-ip)/fs,
                                   baseline_corrected_peak_delay_s=abs(rp_corrected-ip_corrected)/fs,
                                   raw_red_peak=rp,raw_ir_peak=ip,
                                   corrected_red_peak=rp_corrected,corrected_ir_peak=ip_corrected))
    return result


def main():
    OUT.mkdir(exist_ok=True)
    all_results=[];all_points=[];reference_inputs=[]
    fig,axes=plt.subplots(3,2,figsize=(13,10),constrained_layout=True)
    for trial in (1,2,3):
        path=ROOT/f'logs/spo2-20261117/trial-{trial}.csv'
        with path.open(newline='',encoding='utf-8') as f:
            points=[(int(r['session']),int(r['seq']),int(r['read_elapsed_us']),int(r['red_raw']),int(r['ir_raw']),float(r['host_elapsed_s'])) for r in csv.DictReader(f)]
        all_points.append(points)
        t=np.array([p[2]-points[0][2] for p in points])/1e6
        red=np.array([p[3] for p in points],dtype=float)
        ir=np.array([p[4] for p in points],dtype=float)
        fs=(len(t)-1)/(t[-1]-t[0])
        results=[]
        for window in range(5):
            section=[p for p in points if window*6e6<=p[2]-points[0][2]<(window+1)*6e6]
            block=window_estimate(section,window+1)
            debug=debug_window(section)
            groups=[section[i:i+4] for i in range(0,len(section)-3,4)]
            dec_red=np.array([mean(p[3] for p in g) for g in groups])
            dec_ir=np.array([mean(p[4] for p in g) for g in groups])
            x=np.arange(len(dec_red));rr=dec_red-np.polyval(np.polyfit(x,dec_red,1),x)
            ii=dec_ir-np.polyval(np.polyfit(x,dec_ir,1),x)
            freqs=np.fft.rfftfreq(len(rr),d=1/debug['fs'])
            pr=np.abs(np.fft.rfft(rr*np.hanning(len(rr))))**2
            pi=np.abs(np.fft.rfft(ii*np.hanning(len(ii))))**2
            band=(freqs>=.65)&(freqs<=3)
            red_freq=float(freqs[band][np.argmax(pr[band])]);ir_freq=float(freqs[band][np.argmax(pi[band])])
            pulse=(freqs>=.65)&(freqs<=3.5);noise=(freqs>=5)&(freqs<=10)
            block.update(debug=debug,red_spectral_peak_hz=red_freq,ir_spectral_peak_hz=ir_freq,
                         red_pulse_to_high_frequency_energy_db=float(10*np.log10(max(pr[pulse].sum(),1e-12)/max(pr[noise].sum(),1e-12))),
                         ir_pulse_to_high_frequency_energy_db=float(10*np.log10(max(pi[pulse].sum(),1e-12)/max(pi[noise].sum(),1e-12))))
            results.append(block)
        # Non-overlapping 100-point blocks at about 25 Hz for the independent
        # native reference algorithm. No quality thresholds changed in v1.
        groups=[points[i:i+4] for i in range(0,len(points)-3,4)]
        for block in range(len(groups)//100):
            values=[f't{trial}b{block+1}']
            for group in groups[block*100:(block+1)*100]:
                values += [str(round(mean(p[3] for p in group))),str(round(mean(p[4] for p in group)))]
            reference_inputs.append(' '.join(values))
        ax=axes[trial-1,0]
        ax.plot(t,(red/np.median(red)-1)*100,color='#cb3c55',lw=.8,label='Red')
        ax.plot(t,(ir/np.median(ir)-1)*100,color='#087d93',lw=.8,label='IR')
        ax.set(title=f'Trial {trial}: raw baseline and motion',ylabel='Change from median (%)',xlabel='Time (s)')
        for boundary in (6,12,18,24):ax.axvline(boundary,color='#cbd5e1',lw=.7)
        ax.grid(alpha=.2);ax.legend(loc='upper right')
        # First six seconds, original v1 detrending and smoothing. Normalized
        # by each channel DC, not scaled to make amplitudes look identical.
        sel=[p for p in points if p[2]-points[0][2]<6e6]
        gg=[sel[i:i+4] for i in range(0,len(sel)-3,4)]
        rt=smooth([mean(p[3] for p in g) for g in gg]);it=smooth([mean(p[4] for p in g) for g in gg])
        tt=np.array([mean(p[2] for p in g)-points[0][2] for g in gg])/1e6
        ax=axes[trial-1,1]
        ax.plot(tt,np.array(detrend(rt))/mean(rt)*100,color='#cb3c55',lw=1.1,label='Red AC/DC')
        ax.plot(tt,np.array(detrend(it))/mean(it)*100,color='#087d93',lw=1.1,label='IR AC/DC')
        ax.set(title=f'Trial {trial}: first 6 s after v1 processing',ylabel='AC / DC (%)',xlabel='Time (s)')
        ax.grid(alpha=.2);ax.legend(loc='upper right')
        all_results.append(dict(trial=trial,measured_hz=fs,red_min=int(red.min()),red_max=int(red.max()),ir_min=int(ir.min()),ir_max=int(ir.max()),windows=results))
    fig.suptitle('Raw-data investigation: amplitude, drift and pulse detection',fontsize=15)
    fig.savefig(OUT/'raw-diagnosis.png',dpi=160)
    plt.close(fig)
    (OUT/'analysis.json').write_text(json.dumps(all_results,indent=2)+'\n',encoding='utf-8')
    (OUT/'reference-input.txt').write_text('\n'.join(reference_inputs)+'\n',encoding='ascii')
    ablation(all_points)
    gate_fig,gate_axes=plt.subplots(1,2,figsize=(11,3.8),constrained_layout=True)
    for axis,trial in zip(gate_axes,(all_results[0],all_results[2])):
        pi=[w.get('red_pi_rms_pct',float('nan')) for w in trial['windows']]
        bars=axis.bar(range(1,6),pi,color=['#cf4d66' if x<.05 else '#4e819f' for x in pi])
        axis.axhline(.05,color='#9d263b',ls='--',label='v1 fixed cutoff: 0.05%')
        for bar,x in zip(bars,pi):axis.text(bar.get_x()+bar.get_width()/2,x+.002,f'{x:.3f}%',ha='center',fontsize=9)
        axis.set(title=f"Trial {trial['trial']}: Red pulse amplitude gate",xlabel='6-second window',ylabel='Processed Red RMS / DC (%)',xticks=range(1,6))
        axis.set_ylim(0,max(pi)*1.25);axis.legend(fontsize=9);axis.grid(axis='y',alpha=.15)
    gate_fig.savefig(OUT/'fixed-gate.png',dpi=160);plt.close(gate_fig)
    exe=OUT/'reference/reference.exe'
    if exe.exists():
        run=subprocess.run([str(exe)],input='\n'.join(reference_inputs)+'\n',text=True,capture_output=True,check=True)
        (OUT/'reference-output.csv').write_text('block,spo2,reference_spo2_flag,reference_hr,reference_hr_flag\n'+run.stdout,encoding='ascii')
        print(run.stdout)
    for trial in all_results:
        print('trial',trial['trial'])
        for w in trial['windows']:
            print(json.dumps({k:w.get(k) for k in ['window','valid','reasons','red_pi_rms_pct','ir_pi_rms_pct','red_ir_correlation','red_spectral_peak_hz','ir_spectral_peak_hz','red_pulse_to_high_frequency_energy_db','periodicity','accepted_beats']}))
    print('Last window peak timing:',json.dumps(all_results[2]['windows'][4]['debug']))


if __name__=='__main__':
    main()
