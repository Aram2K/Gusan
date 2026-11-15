"""Procedurally generated upbeat background track (CC0: produced by this script, no samples).
Usage: python music.py OUT.wav SECONDS"""
import sys, numpy as np
SR=48000; BPM=124; BEAT=60/BPM; BAR=4*BEAT
def note(n): return 440*2**((n-69)/12)
CHORDS=[[60,64,67,72],[55,59,62,67],[57,60,64,69],[53,57,60,65]]   # C  G  Am  F
ROOTS=[36,43,45,41]
LEAD=[[72,74,76,79,76,74,72,None],[79,76,74,76,None,74,72,None],[69,72,76,77,76,72,None,None],[77,76,74,72,74,None,None,None]]
def env(n,a,d,sus=0.0,r=0.05):
    e=np.full(n,sus,np.float32); ai=min(int(a*SR),n); di=min(int(d*SR),max(0,n-ai)); ri=min(int(r*SR),n)
    if ai: e[:ai]=np.linspace(0,1,ai,endpoint=False)
    if di: e[ai:ai+di]=np.linspace(1,sus,di)
    if ri: e[-ri:]*=np.linspace(1,0,ri)
    return e
def osc(f,n,harm):
    t=np.arange(n)/SR; return sum(a*np.sin(2*np.pi*f*k*t+ph) for k,a,ph in harm)
def pluck(f,n):  return osc(f,n,[(1,1,0),(2,.3,0),(3,.1,0)])*env(n,0.003,0.3,0,0.02)*0.45
def pad(f,n):    return (osc(f,n,[(1,1,0),(2,.45,.4),(3,.2,.9),(4,.08,0)])+osc(f*1.003,n,[(1,.8,1.1)]))*env(n,0.4,0.4,0.85,0.35)*0.15
def bass(f,n):   return osc(f,n,[(1,1,0),(2,.35,0)])*env(n,0.004,0.25,0.35,0.05)*0.65
def lead(f,n):
    t=np.arange(n)/SR; vib=1+0.004*np.sin(2*np.pi*5.5*t)*np.minimum(1,t*4)
    return osc(f*1.0,n,[(1,1,0),(2,.25,0),(3,.08,0)])*np.cos(0*t)*env(n,0.02,0.35,0.55,0.08)*0.42*vib
def kick(n):
    t=np.arange(n)/SR; f=48+120*np.exp(-t*30)
    return np.sin(2*np.pi*np.cumsum(f)/SR)*np.exp(-t*8)*0.95
def fftfilt(x,lo=None,hi=None):
    X=np.fft.rfft(x); f=np.fft.rfftfreq(len(x),1/SR); g=np.ones_like(f)
    if lo: g*=1/(1+(lo/np.maximum(f,1))**4)
    if hi: g*=1/(1+(f/hi)**4)
    return np.fft.irfft(X*g,len(x))
def hat(n,open_=False,seed=1):
    t=np.arange(n)/SR; x=np.random.default_rng(seed).normal(0,1,n)
    return fftfilt(x,lo=7000,hi=14000)*np.exp(-t*(14 if open_ else 55))*0.09
def clap(n):
    t=np.arange(n)/SR; x=np.random.default_rng(7).normal(0,1,n); e=np.zeros(n)
    for k,off in enumerate([0,0.011,0.024]):
        i=int(off*SR); e[i:]+=np.exp(-(t[:n-i])*(40 if k<2 else 16))
    return fftfilt(x*e,lo=900,hi=6500)*0.3
def main(out,secs):
    N=int(secs*SR); L=np.zeros(N+SR*3); R=np.zeros(N+SR*3)
    def put(start,sig,pan=0.0):
        i=int(start*SR); j=min(i+len(sig),len(L))
        if i>=len(L): return
        s=sig[:j-i]; L[i:j]+=s*(1-pan)/2; R[i:j]+=s*(1+pan)/2
    bar=0; t=0.0
    while t<secs:
        ch=CHORDS[bar%4]; root=ROOTS[bar%4]; nb=int(BAR*SR)
        for k,m in enumerate(ch): put(t,pad(note(m),nb),(k-1.5)*0.3)
        for b in [0,1.5,2,3.5]: put(t+b*BEAT,bass(note(root),int(BEAT*0.9*SR)))
        seq=[ch[0],ch[1],ch[2],ch[3],ch[1]+12,ch[2]+12,ch[3],ch[2]]
        for e,m in enumerate(seq): put(t+e*BEAT/2,pluck(note(m),int(BEAT*0.9*SR)),0.4*np.sin(e*1.3))
        if bar>=1:
            for b in range(4):
                put(t+b*BEAT,kick(int(0.3*SR)))
                put(t+b*BEAT,hat(int(0.1*SR),seed=b+1),0.25)
                put(t+(b+0.5)*BEAT,hat(int(0.22*SR),open_=(b==3),seed=b+9),-0.25)
            for b in [1,3]: put(t+b*BEAT,clap(int(0.3*SR)),0.1)
        if bar>=4 and (bar%8)<6:
            for e,m in enumerate(LEAD[bar%4]):
                if m is None: continue
                n=int(BEAT*0.55*SR); s=lead(note(m),n); put(t+e*BEAT/2,s,0.15)
                put(t+e*BEAT/2+BEAT*0.75,s*0.35,-0.35); put(t+e*BEAT/2+BEAT*1.5,s*0.14,0.35)   # echo
        bar+=1; t+=BAR
    mix=np.stack([fftfilt(L[:N],hi=11000),fftfilt(R[:N],hi=11000)],1)
    mix/=np.max(np.abs(mix))+1e-9; mix*=0.85
    fade=int(1.5*SR); mix[:fade]*=np.linspace(0,1,fade)[:,None]; mix[-int(2.5*SR):]*=np.linspace(1,0,int(2.5*SR))[:,None]
    import wave
    with wave.open(out,'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes((np.clip(mix,-1,1)*32767).astype(np.int16).tobytes())
if __name__=='__main__': main(sys.argv[1],float(sys.argv[2]))
