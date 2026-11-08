<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Gusan iOS — plan and mockups v2</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--blue:#0a84ff;--blue2:#0066d6;--ink:#111;--mute:#6b7280;--line:#e5e7eb;--bg:#f3f4f8;--ok:#1f9d55;--warn:#d97706}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:21px;margin:44px 0 6px;padding-top:10px;border-top:1px solid var(--line)}
p{margin:6px 0;max-width:840px}.sub{color:var(--mute)}
.row{display:flex;flex-wrap:wrap;gap:26px;align-items:flex-start}
.shot{width:340px}.shot .cap{font-size:13px;margin-top:10px;color:var(--mute);line-height:1.4}.shot .cap b{color:var(--ink);display:block;font-size:14px;margin-bottom:2px}
.phone{width:340px;height:712px;border-radius:50px;background:#111;padding:9px;box-shadow:0 14px 40px rgba(0,0,0,.22);position:relative}
.scr{width:100%;height:100%;border-radius:42px;overflow:hidden;background:#fff;position:relative;display:flex;flex-direction:column}
.island{position:absolute;top:11px;left:50%;transform:translateX(-50%);width:96px;height:28px;border-radius:16px;background:#000;z-index:9}
.status{height:46px;display:flex;justify-content:space-between;align-items:flex-end;padding:0 28px 4px;font-size:13px;font-weight:600}
.status .back{color:var(--blue);font-weight:500;font-size:12px}
/* host chat */
.host{flex:1;display:flex;flex-direction:column;background:#efe7dd;min-height:0}
.chatbar{background:#f6f6f6;border-bottom:1px solid #ddd;padding:8px 12px;font-size:14px;font-weight:600;display:flex;gap:10px;align-items:center}
.av{width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,#c7d2fe,#93c5fd)}
.msgs{flex:1;padding:12px;display:flex;flex-direction:column;gap:7px;font-size:13px;overflow:hidden}
.m{max-width:80%;padding:7px 10px;border-radius:12px;background:#fff;box-shadow:0 1px 0 rgba(0,0,0,.06)}.m.me{align-self:flex-end;background:#dcf8c6}
.field{background:#f6f6f6;border-top:1px solid #ddd;padding:7px 10px;display:flex;gap:8px;align-items:center}
.field .in{flex:1;background:#fff;border:1px solid #ddd;border-radius:18px;padding:7px 12px;font-size:13.5px;min-height:32px;color:#9ca3af}
.field .in.txt{color:#111}.caret{display:inline-block;width:2px;height:14px;background:var(--blue);vertical-align:-2px;margin-left:1px;border-radius:1px}
.sel{background:#b3d7ff}
/* iOS keyboard */
.kb{background:#d1d4da;padding:0 0 4px;flex:none}
.strip{height:50px;background:#d1d4da;display:flex;align-items:center;gap:8px;padding:6px 8px}
.keys{padding:4px 3px 0;display:flex;flex-direction:column;gap:9px}
.kr{display:flex;justify-content:center;gap:5px}
.k{width:29px;height:38px;border-radius:5px;background:#fff;box-shadow:0 1px 0 #898a8d;font-size:17px;display:flex;align-items:center;justify-content:center;font-weight:400}
.k.f{background:#abb0ba;font-size:12px;width:40px}.k.sp{width:170px;font-size:14px}.k.ret{width:80px;font-size:14px;background:#abb0ba}.k.sm{width:40px;background:#abb0ba;font-size:15px}
.home{height:22px;display:flex;align-items:center;justify-content:center}.home i{width:120px;height:5px;border-radius:3px;background:#111}
/* gusan strip content */
.gchip{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.85);border-radius:999px;padding:4px 10px 4px 4px;font-size:12.5px;font-weight:600;color:#111;box-shadow:0 1px 0 rgba(0,0,0,.08)}
.gchip img{width:22px;height:22px;border-radius:50%}
.gchip.agent{background:#111827;color:#fff}
.gmic{flex:1;height:38px;border-radius:999px;background:linear-gradient(180deg,#2f8fff,#0a6fe0);box-shadow:0 2px 8px rgba(10,111,224,.35),inset 0 1px 0 rgba(255,255,255,.3);display:flex;align-items:center;justify-content:center;gap:8px;color:#fff;font-size:13.5px;font-weight:600}
.gmic svg{width:16px;height:16px}
.gbtn{width:38px;height:38px;border-radius:999px;background:rgba(255,255,255,.85);display:flex;align-items:center;justify-content:center;box-shadow:0 1px 0 rgba(0,0,0,.08)}
.gbtn svg{width:18px;height:18px}
.gbtn.micb{background:linear-gradient(180deg,#2f8fff,#0a6fe0);box-shadow:0 2px 8px rgba(10,111,224,.35)}.gbtn.micb svg{width:17px;height:17px}.gbtn.cam svg{width:19px;height:19px}.gbtn.cam.on{background:#dbeafe;box-shadow:inset 0 0 0 1.5px var(--blue)}.ctx{display:flex;align-items:center;gap:3px;background:rgba(255,255,255,.85);border-radius:999px;padding:5px 8px;box-shadow:0 1px 0 rgba(0,0,0,.08);color:#0b3d91;font-size:11px;font-weight:700}.ctx svg{width:15px;height:15px}.ctx span{color:#6b7280}.gbtn.x svg{width:14px;height:14px}.gbtn.on{background:#dbeafe;box-shadow:inset 0 0 0 1.5px var(--blue)}
.gtxt{flex:1;font-size:13.5px;font-weight:600;color:#0b3d91;display:flex;align-items:center;gap:8px;min-width:0}
.gtxt small{font-weight:500;color:#4b5563;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wave{display:flex;align-items:center;gap:2.5px;height:28px}.wave.full{flex:1;justify-content:space-between;gap:0;height:38px;min-width:0;overflow:hidden}.wave.full i{width:3.5px}.wave i{display:block;width:3px;border-radius:2px;background:var(--blue);height:var(--h)}
.pillb{border-radius:999px;padding:8px 14px;font-size:13px;font-weight:600;border:0;background:#fff;color:#111;box-shadow:0 1px 0 rgba(0,0,0,.08)}
.pillb.pri{background:#111827;color:#fff}.pillb.blue{background:var(--blue);color:#fff}
.spin{display:inline-block;width:16px;height:16px;border-radius:50%;border:2.5px solid #bfdbfe;border-top-color:var(--blue);flex:none;animation:sp 1s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}.dots i{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--blue);margin-right:3px;opacity:.45}.dots i:nth-child(2){opacity:.75}.dots i:nth-child(3){opacity:1}
/* glass card over keys */
.gcard{margin:0 8px 8px;background:rgba(255,255,255,.92);backdrop-filter:blur(14px);border-radius:16px;padding:12px 14px;font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,.12)}
.gcard b{display:block;font-size:14px;margin-bottom:2px}.gcard .k2{color:#4b5563;font-size:12.5px}
.gcard .acts{display:flex;gap:8px;margin-top:10px;align-items:center}.gcard .acts .sp{flex:1}
.prog{height:4px;border-radius:2px;background:#e5e7eb;overflow:hidden;margin-top:8px}.prog i{display:block;height:100%;width:45%;background:var(--blue)}
/* app */
.app{flex:1;display:flex;flex-direction:column;background:#f2f2f7;min-height:0}
.nav{padding:10px 18px 8px;font-size:30px;font-weight:700;display:flex;align-items:center;gap:10px;letter-spacing:-.3px}.nav img{width:34px;height:34px;border-radius:9px}.nav .sm{font-size:13px;color:var(--mute);font-weight:500;margin-left:auto}
.body{flex:1;padding:4px 16px;display:flex;flex-direction:column;gap:12px;overflow:hidden}
.seg{display:flex;background:#e3e4ea;border-radius:12px;padding:3px}.seg span{flex:1;text-align:center;font-size:14px;padding:8px;border-radius:9px;color:#333}.seg span.on{background:#fff;font-weight:600;box-shadow:0 1px 4px rgba(0,0,0,.12);color:#000}
.hero{background:linear-gradient(160deg,#0a84ff,#0b4fc0);color:#fff;border-radius:18px;padding:16px;font-size:13.5px}.hero b{display:block;font-size:19px;margin-bottom:4px;letter-spacing:-.2px}.hero .hsub{opacity:.92;display:block;color:#fff}
.hero .btn{display:inline-block;margin-top:10px;background:#fff;color:#0b4fc0;border-radius:999px;padding:7px 14px;font-weight:600;font-size:13px}
.group{background:#fff;border-radius:14px;overflow:hidden}
.cell{padding:11px 14px;font-size:14.5px;display:flex;gap:12px;align-items:center;border-bottom:1px solid #eee}.cell:last-child{border-bottom:0}
.cell .t{flex:1;min-width:0}.cell .t small{display:block;color:var(--mute);font-size:12.5px}.cell .chev{color:#c7c7cc;font-size:18px}
.cell .ic{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#fff;flex:none}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}.dot.run{background:var(--blue);box-shadow:0 0 0 3px #dbeafe}.dot.ok{background:var(--ok)}.dot.wait{background:var(--warn)}.dot.off{background:#c7c7cc}
.tabs{height:78px;background:#f9f9f9;border-top:1px solid #ddd;display:flex;justify-content:space-around;align-items:flex-start;padding-top:9px;font-size:10.5px;color:#8e8e93;flex:none}
.tabs div{display:flex;flex-direction:column;align-items:center;gap:4px;width:64px}.tabs div.on{color:var(--blue)}.tabs svg{width:24px;height:24px}
.toggle{width:46px;height:28px;border-radius:14px;background:#34c759;position:relative;flex:none}.toggle:after{content:"";position:absolute;right:2px;top:2px;width:24px;height:24px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2)}.toggle.off{background:#e9e9eb}.toggle.off:after{right:auto;left:2px}
.chat{flex:1;display:flex;flex-direction:column;gap:7px;font-size:13px;padding:0 2px;overflow:hidden}
.b{max-width:86%;padding:8px 11px;border-radius:14px;background:#fff}.b.me{align-self:flex-end;background:var(--blue);color:#fff}.b.sys{background:transparent;color:var(--mute);font-size:11.5px;padding:0 4px}
.cin{background:#fff;border-radius:20px;padding:9px 14px;font-size:13.5px;color:#999;border:1px solid #ddd;display:flex;justify-content:space-between;align-items:center}
.hop{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:radial-gradient(circle at 50% 30%,#e8f1ff,#fff 70%)}
.hop .wave{height:70px}.hop .wave i{width:6px;border-radius:3px}
.hop h4{margin:0;font-size:24px;letter-spacing:-.3px}.hop p{font-size:14px;color:var(--mute);text-align:center;margin:0 36px}
.island.rec{width:132px;display:flex;align-items:center;gap:6px;padding:0 9px;overflow:visible}.island.rec img{width:16px;height:16px;border-radius:4px;position:relative;z-index:2}.island.rec .mw{display:flex;align-items:center;gap:2px;height:16px;position:relative;z-index:2}.island.rec .mw i{width:2px;border-radius:1px;background:#fff;height:var(--h);max-height:14px;transform:scaleY(.5);animation:mw .9s ease-in-out infinite alternate}.island.rec .mw i:nth-child(odd){animation-duration:1.2s}@keyframes mw{to{transform:scaleY(1)}}.island.rec .ring{position:absolute;inset:-3px;border-radius:19px;border:2px solid #0a84ff;box-shadow:0 0 10px 2px rgba(10,132,255,.75),inset 0 0 6px rgba(10,132,255,.5);animation:glow 1.3s ease-in-out infinite;z-index:1}@keyframes glow{0%,100%{opacity:.45;inset:-2px}50%{opacity:1;inset:-4px}}
.island.la{width:150px;display:flex;align-items:center;gap:7px;padding:0 8px;color:#fff;font-size:11px;font-weight:700}.island.la img{width:18px;height:18px;border-radius:5px}.island.la i{margin-left:auto;width:8px;height:8px;border-radius:50%;background:#0a84ff;box-shadow:0 0 6px #0a84ff}
.scr.lock{background:linear-gradient(180deg,#0f1b3d,#2a2f5a 60%,#4a3b6b)}.ltime{color:#fff;font-size:68px;font-weight:600;text-align:center;margin-top:62px;letter-spacing:-2px;line-height:1}.ldate{color:#fff;text-align:center;font-size:15px;font-weight:500;opacity:.9;margin-bottom:22px}
.lcard{margin:0 12px 10px;background:rgba(255,255,255,.14);backdrop-filter:blur(20px);border-radius:20px;padding:12px 14px;color:#fff;font-size:12.5px}.lcard .lhead{display:flex;align-items:center;gap:7px;font-size:12px;opacity:.9}.lcard .lhead img{width:20px;height:20px;border-radius:6px}.lcard .lhead span{margin-left:auto}.ltitle{font-weight:700;font-size:14px;margin-top:6px}.lsub{opacity:.8;margin-top:2px}.lcard .prog{background:rgba(255,255,255,.2)}.lcard .prog i{background:#fff}.lacts{display:flex;gap:8px;margin-top:10px;justify-content:flex-end}.lhint{color:rgba(255,255,255,.6);text-align:center;font-size:11px;margin-top:4px}.lbottom{position:absolute;bottom:22px;left:0;right:0;display:flex;justify-content:space-between;padding:0 34px}.lbottom i{width:46px;height:46px;border-radius:50%;background:rgba(255,255,255,.18)}
.notif{position:absolute;top:58px;left:14px;right:14px;background:rgba(255,255,255,.96);border-radius:18px;padding:10px 13px;font-size:12.5px;box-shadow:0 8px 24px rgba(0,0,0,.2);z-index:8;display:flex;gap:10px;align-items:flex-start}.notif img{width:34px;height:34px;border-radius:9px}
.mail{flex:1;background:#fff;font-size:13px;display:flex;flex-direction:column}.mail .hd{padding:10px 14px;border-bottom:1px solid #eee;font-weight:700;font-size:20px}.mrow{padding:10px 14px;border-bottom:1px solid #f0f0f0}.mrow b{display:block;font-size:13.5px}.mrow .s{color:#111}.mrow .p{color:#6b7280;font-size:12.5px}
table{border-collapse:collapse;width:100%;font-size:13.5px;max-width:1000px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600;font-size:12.5px}
.tag{display:inline-block;font-size:11.5px;font-weight:600;border-radius:6px;padding:2px 8px;background:#e0e7ff;color:#3730a3;white-space:nowrap}
.note{background:#fff;border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:8px;padding:12px 16px;max-width:840px;font-size:14px}
ol,ul{max-width:840px}li{margin:4px 0}
</style></head><body><div class="wrap">
<h1>Gusan on iPhone — plan and mockups, v2</h1>
<p class="sub">Your keyboard, exactly as Apple's, with a Gusan strip on top. It appears whenever the keyboard opens, in every app.</p>

<h2>1. Where Gusan lives</h2>
<p>iOS lets nothing sit on top of another app's keyboard. The only way a Gusan strip can appear every time a keyboard opens is for Gusan to <b>be</b> the keyboard. So: a pixel-faithful copy of the iPhone keyboard (same keys, sizes, sounds, autocorrect behaviour where iOS allows it), and the row where Apple shows word suggestions becomes the <b>Gusan strip</b>. One honest difference: Apple's dictation key (the mic in the bottom row) exists only on Apple's own keyboard; iOS gives custom keyboards no microphone, so that corner is empty and the Gusan mic in the strip is the mic.</p>
<div class="note"><b>The strip, idle:</b> mode chip (Smart or Agent) on the left; screen and mic icons on the right. Three things. The mic is the default: tap it and recording starts. The screen icon is selected by default: a screenshot is taken when you start and when you finish, so Gusan knows what's on screen. Tap it to deselect for a private moment; tap again to select. Everything Gusan does happens in that strip, or in one glass card that slides over the top key rows; the keyboard never disappears, and you never leave the app you're in.</div>

<div class="note" style="margin-top:10px"><b>Context rule:</b> text you have selected is <b>always</b> sent as context, and the result replaces it. With the screen icon selected, Gusan gets the selection <b>and</b> the two screenshots. With nothing selected it uses the text around your cursor (and the screenshots, if the screen icon is selected). The strip shows what it's using while you speak. <b>Listening indicator:</b> while recording, the Dynamic Island glows (pulsing blue ring + waveform), so you can see Gusan is listening even with the keyboard half hidden. <b>Connections</b> add knowledge: with Google Calendar (or the iPhone Calendar) connected, "reply that I'm free" is answered from your real calendar, in Smart mode instantly and in Agent mode for booking.</div>
<h2>2. The strip, state by state</h2>
<div class="row">

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Anna</div>
<div class="msgs"><div class="m">Hey! Are you free for a call tomorrow at 3? We need to finalize the budget.</div><div class="m me">Let me check</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip"><img src="LOGO">Smart</div><div style="flex:1"></div><div class="gbtn cam on">SCRSVG</div><div class="gbtn micb">MICSVG</div></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>1 · Idle</b>Your normal keyboard. The Gusan strip replaces Apple's suggestion row: mode chip, screen (selected by default), mic. Tap the mic to start.</div></div>

<div class="shot"><div class="phone"><div class="island rec"><span class="ring"></span><img src="LOGO"><div class="mw">WAVE</div></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Anna</div>
<div class="msgs"><div class="m">Hey! Are you free for a call tomorrow at 3? We need to finalize the budget.</div><div class="m me">Let me check</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="wave full">WAVEFULL</div><div class="gbtn x">XSVG</div><div class="gbtn micb">MICSVG</div></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>2 · Listening</b>Chime, then the waveform fills the strip, and the Dynamic Island lights up with a pulsing blue ring and a small waveform for as long as Gusan listens. Two icons on the right: X cancels, the mic finishes and sends. With the screen icon selected, a frame was taken at start and one more at finish.</div></div>

<div class="shot"><div class="phone"><div class="island rec"><span class="ring"></span><img src="LOGO"><div class="mw">WAVE</div></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Marc Dupont</div>
<div class="msgs"><div class="m">Following our call, here is the draft agreement. Could you confirm the revenue split by Friday?</div></div>
<div class="field"><div class="in txt">Hi Marc, <span class="sel">thanks for sending the draft we will look at it and come back</span></div></div></div>
<div class="kb"><div class="strip"><div class="ctx">CTXSEL<span>+</span>CTXSCR</div><div class="wave full">WAVEFULL</div><div class="gbtn x">XSVG</div><div class="gbtn micb">MICSVG</div></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>2b · Listening with a selection, screen selected</b>The small chip shows what Gusan is using: your selected text and the screenshots. Say "make this formal and confirm Friday": the selection is replaced, and the screen tells it what Friday is about.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Anna</div>
<div class="msgs"><div class="m">Hey! Are you free for a call tomorrow at 3? We need to finalize the budget.</div><div class="m me">Let me check</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gtxt"><span class="spin"></span><small>"reply that 3 works, ask her to send the numbers before"</small></div><button class="pillb">Cancel</button></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>3 · Working</b>Second chime when you tap the mic. A small spinner and your words, so you know it heard you. 2–4 seconds.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Anna</div>
<div class="msgs"><div class="m">Hey! Are you free for a call tomorrow at 3? We need to finalize the budget.</div><div class="m me">Let me check</div></div>
<div class="field"><div class="in txt">3 works for me! Could you send the latest numbers before the call so we can go straight to decisions?<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip"><img src="LOGO">Smart</div><div class="gtxt" style="color:var(--ok)">Inserted</div><button class="pillb">Undo</button><div class="gbtn cam on">SCRSVG</div><div class="gbtn micb">MICSVG</div></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>4 · Done</b>The text is at your cursor. Undo stays for 30 s, then disappears; the mic is ready again right away. If you had selected text, exactly that was replaced.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Travel group</div>
<div class="msgs"><div class="m">Can you book the Friday flight to Paris?</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip agent"><img src="LOGO">Agent</div><div class="gtxt">Finding flights <small>· step 2 of 5</small></div><button class="pillb">Stop</button></div>
<div class="gcard"><b>Comparing Air France and Wizz for Fri 14 Nov</b><span class="k2">Working in a cloud browser. You can keep typing or leave; I'll notify you.</span><div class="prog"><i></i></div></div>
KEYS2<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>5 · Agent started</b>The card shows for 3 seconds so you know it understood, then collapses. From here the task lives in the Dynamic Island (5b, 5c).</div></div>

<div class="shot"><div class="phone"><div class="island la"><img src="LOGO"><span>3/5</span><i></i></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Travel group</div>
<div class="msgs"><div class="m">Can you book the Friday flight to Paris?</div><div class="m me">On it, give me a minute</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip agent"><img src="LOGO">Agent</div><div style="flex:1"></div><div class="gbtn cam on">SCRSVG</div><div class="gbtn micb">MICSVG</div></div>
KEYS<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>5b · While the agent works</b>Nothing covers your keyboard. The task sits in the Dynamic Island with its step count; tap it to open the session. You keep chatting, or leave the app.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr lock">
<div class="ltime">9:41</div><div class="ldate">Friday 14 November</div>
<div class="lcard"><div class="lhead"><img src="LOGO"><b>Gusan · Agent</b><span>step 3 of 5</span></div><div class="ltitle">Booking Yerevan → Paris, Fri 14 Nov</div><div class="lsub">Comparing Air France and Wizz · will ask before paying</div><div class="prog"><i style="width:60%"></i></div></div>
<div class="lcard done"><div class="lhead"><img src="LOGO"><b>Gusan · Agent</b><span>done · 2 min</span></div><div class="ltitle">Sony XM6 best US price: $379.99 at Best Buy</div><div class="lsub">2 sources · Amazon $399 · Best Buy $379.99</div><div class="lacts"><button class="pillb">Copy</button><button class="pillb pri">Open</button></div></div>
<div class="lhint">swipe to hide · stays in Sessions</div>
<div class="lbottom"><i></i><i></i></div>
</div></div><div class="cap"><b>5c · Live Activity</b>On the lock screen and in the Dynamic Island until the task is done; you can't lose it. When finished: the result, Copy, and Open (goes to the session chat). Swiping it away only hides it; the session stays in the app.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Travel group</div>
<div class="msgs"><div class="m">Can you book the Friday flight to Paris?</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip agent"><img src="LOGO">Agent</div><div class="gtxt">Needs your OK</div></div>
<div class="gcard"><b>Book Air France AF1061 · €128</b><span class="k2">Fri 14 Nov, 07:10 → 11:05 · 1 adult · economy · airfrance.com</span><div class="acts"><button class="pillb">Cancel</button><div class="sp"></div><button class="pillb">Details</button><button class="pillb pri">Confirm</button></div></div>
KEYS2<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>6 · Confirm</b>Anything that buys, sends or submits stops here. Shown in the strip if your keyboard is open; otherwise as a notification and in the Live Activity. Confirm never appears where Cancel was. Expires in 15 min.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="host"><div class="chatbar"><div class="av"></div>Travel group</div>
<div class="msgs"><div class="m">Can you book the Friday flight to Paris?</div></div>
<div class="field"><div class="in">Message<span class="caret"></span></div></div></div>
<div class="kb"><div class="strip"><div class="gchip agent"><img src="LOGO">Agent</div><div class="gtxt">Needs a login</div></div>
<div class="gcard"><b>airfrance.com asks you to sign in</b><span class="k2">Sign in with your saved Air France login? Gusan fills it in for the agent; the agent never sees the password.</span><div class="acts"><button class="pillb">Cancel</button><div class="sp"></div><button class="pillb">Pick another</button><button class="pillb pri">Face ID · Sign in</button></div></div>
KEYS2<div class="home"><i></i></div></div>
</div></div><div class="cap"><b>7 · Needs a login</b>The agent pauses. Face ID approves; the password is typed into the cloud browser by Gusan's server, never shown to the agent or in the chat. First time for a site: pick it from your iPhone Passwords (see Vault).</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span class="back">‹ WhatsApp</span><span>9:41</span><span>▮</span></div>
<div class="hop"><img src="LOGO" style="width:72px;height:72px;border-radius:18px;box-shadow:0 8px 24px rgba(0,0,0,.15)">
<div class="wave">WAVEBIG</div>
<h4>Listening…</h4><p>Keep talking. Tap <b>‹ WhatsApp</b> top left to go back; Gusan keeps listening.</p><button class="pillb">Stop</button></div>
</div></div><div class="cap"><b>0 · First tap of a session only</b>iPhone keyboards can't use the mic, so Gusan opens for a moment and goes right back. One screen, one line. Every later tap records in the strip.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="notif"><img src="LOGO"><div><b>Gusan</b> · now<br>Your reply to Marc is ready and copied. Tap a text field to insert it.</div></div>
<div class="mail"><div class="hd">Inbox</div>
<div class="mrow"><b>Marc Dupont</b><span class="s">Partnership proposal</span><div class="p">Hi Aram, following our call, here is the draft agreement. Could you confirm the revenue split by Friday?…</div></div>
<div class="mrow"><b>Apple</b><span class="s">Your receipt</span><div class="p">Receipt for your purchase…</div></div>
<div class="mrow"><b>Supabase</b><span class="s">Usage report</span><div class="p">Your project hftcelxz…</div></div></div>
</div></div><div class="cap"><b>Back Tap · no keyboard open</b>Double-tap the back of the phone anywhere: screenshot + listen + do. Result copied and announced; the strip inserts it when you open a field.</div></div>
</div>

<h2>3. The app</h2>
<p class="sub">Four tabs. Everything that the strip doesn't need is gone: no preset actions, no How to use tab, no long onboarding.</p>
<div class="row">

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="app"><div class="nav"><img src="LOGO">Gusan<span class="sm">Pro</span></div>
<div class="body">
<div class="seg"><span class="on">Smart</span><span>Agent</span></div>
<div class="hero"><b>Ready in every app</b><span class="hsub">Keyboard added · Full Access · Microphone — all set. Open any app and tap the mic.</span></div>
<div class="group">
<div class="cell"><div class="ic" style="background:#0a84ff">SCREENSVGW</div><div class="t">Gusan Screen<small>On · lets the keyboard see what's on screen when you speak</small></div><div class="toggle"></div></div>
<div class="cell"><div class="ic" style="background:#34a853">C</div><div class="t">Connections<small>Google Calendar connected · add Contacts, Gmail</small></div><span class="chev">›</span></div>
<div class="cell"><div class="ic" style="background:#8e8e93">BT</div><div class="t">Back Tap<small>Not set up · takes 1 minute</small></div><span class="chev">›</span></div></div>
<div class="group"><div class="cell"><div class="t"><small style="margin-bottom:2px">Last result · 2 min ago · WhatsApp</small>"3 works for me! Could you send the latest numbers before the call…"</div><span class="chev">›</span></div></div>
</div>
TABS1</div>
</div></div><div class="cap"><b>Home</b>Mode, setup state, Gusan Screen switch, Connections (calendar first), Back Tap, last result.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="app"><div class="nav">Sessions</div>
<div class="body"><div class="seg"><span>Smart</span><span class="on">Agent</span></div><div class="group">
<div class="cell"><span class="dot run"></span><div class="t">Book Yerevan → Paris, Fri 14 Nov<small>Running · step 3 of 5 · will need your OK</small></div><span class="chev">›</span></div>
<div class="cell"><span class="dot wait"></span><div class="t">Cancel the Bolt subscription<small>Waiting for your confirmation · 12 min left</small></div><span class="chev">›</span></div>
<div class="cell"><span class="dot ok"></span><div class="t">Sony XM6 — best US price<small>Done · $379.99 at Best Buy · 2 sources</small></div><span class="chev">›</span></div>
<div class="cell"><span class="dot ok"></span><div class="t">Reply to Marc about the revenue split<small>Done · inserted in Gmail · yesterday</small></div><span class="chev">›</span></div>
<div class="cell"><span class="dot off"></span><div class="t" style="color:#6b7280">Find a dentist near Komitas<small>Stopped by you · Tue</small></div><span class="chev">›</span></div></div>
</div>
TABS2</div>
</div></div><div class="cap"><b>Sessions</b>Same Smart / Agent switch as Home. Agent: running, waiting, done sessions. Smart: your results, newest first. Nothing gets lost.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span class="back">‹ Sessions</span><span>9:41</span><span>▮</span></div>
<div class="app"><div class="nav" style="font-size:17px">Book Yerevan → Paris<span class="sm"><span class="dot run"></span> running</span></div>
<div class="body"><div class="chat">
<div class="b me">Book me on the Friday morning flight to Paris, economy, under 150 euro.</div>
<div class="b sys">Screenshot from WhatsApp attached · 09:41</div>
<div class="b">Searching airfrance.com and wizzair.com for Fri 14 Nov…</div>
<div class="b">Two options. Cheapest: Air France AF1061, 07:10, €128. Wizz is €96 but lands at Beauvais at 23:50.</div>
<div class="b me">Take Air France.</div>
<div class="b" style="box-shadow:0 0 0 1px #e5e7eb"><b>Book AF1061 · €128 — needs your OK</b><br><span style="color:var(--mute)">Fri 14 Nov 07:10 · 1 adult · economy</span><div style="margin-top:8px;display:flex;gap:8px"><button class="pillb" style="box-shadow:0 0 0 1px #ddd">Cancel</button><button class="pillb pri">Confirm</button></div></div>
</div>
<div class="cin">Message the agent…<span style="color:var(--blue);font-weight:600">Speak</span></div>
<div style="display:flex;justify-content:space-between;font-size:12px;color:var(--mute);padding:2px 4px 6px"><span>Remembers: passport name, aisle seat</span><span style="color:#dc2626;font-weight:600">Stop session</span></div>
</div></div>
</div></div><div class="cap"><b>Session</b>Chat with the running agent, confirm, continue later, or stop it. Continue the same session from the strip too.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="app"><div class="nav">Vault<span class="sm">Face ID</span></div>
<div class="body">
<p style="font-size:12.5px;color:var(--mute);margin:0 4px">Logins Gusan may type in for the agent, each time after Face ID. Encrypted on your phone; the agent never sees them.</p>
<div class="group">
<div class="cell"><div class="ic" style="background:#002157">AF</div><div class="t">Air France<small>aram@…com · used today</small></div><span class="chev">›</span></div>
<div class="cell"><div class="ic" style="background:#003580">B.</div><div class="t">Booking.com<small>aram@…com</small></div><span class="chev">›</span></div>
<div class="cell"><div class="ic" style="background:#34a853">G</div><div class="t">Google<small>Sign in with Google · you approve each time</small></div><span class="chev">›</span></div>
<div class="cell" style="color:var(--blue);font-weight:600"><div class="ic" style="background:#dbeafe;color:var(--blue)">+</div><div class="t">Add from iPhone Passwords<small style="color:var(--mute);font-weight:400">Pick a saved login with Face ID</small></div></div></div>
<div class="group"><div class="cell"><div class="t">Always ask before using a login<small>Recommended</small></div><div class="toggle"></div></div></div>
</div>
TABS3</div>
</div></div><div class="cap"><b>Vault</b>Logins picked from your iPhone Passwords, one tap + Face ID each. Locked with Face ID. The agent never sees a password. No payment cards in v1.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="app"><div class="nav">Settings</div>
<div class="body">
<div class="group"><div class="cell"><div class="ic" style="background:#0a84ff">A</div><div class="t">Aram Atamian<small>Gusan Pro · aramatamian15@gmail.com</small></div><span class="chev">›</span></div></div>
<div class="group">
<div class="cell"><div class="t">Language I speak<small>Automatic</small></div><span class="chev">›</span></div>
<div class="cell"><div class="t">Sounds<small>Chime at start and stop</small></div><div class="toggle"></div></div>
<div class="cell"><div class="t">Notifications<small>When a session finishes or needs you</small></div><div class="toggle"></div></div>
<div class="cell"><div class="t">Privacy Mode<small>No web, no screen, no agents</small></div><div class="toggle off"></div></div></div>
<div class="group">
<div class="cell"><div class="t">Connections<small>Google Calendar · iPhone Calendar · Contacts</small></div><span class="chev">›</span></div>
<div class="cell"><div class="t">Keyboard setup<small>Add Gusan · Full Access · Back Tap</small></div><span class="chev">›</span></div>
<div class="cell"><div class="t">Advanced<small>Diagnostics · storage fallback · idle time</small></div><span class="chev">›</span></div></div>
</div>
TABS4</div>
</div></div><div class="cap"><b>Settings</b>Eight rows. Diagnostics and the Sideloadly workarounds live under Advanced.</div></div>

<div class="shot"><div class="phone"><div class="island"></div><div class="scr">
<div class="status"><span>9:41</span><span>●●● ▲ ▮</span></div>
<div class="app"><div class="body" style="padding-top:14px">
<div class="hero" style="padding:22px 18px"><img src="LOGO" style="width:56px;height:56px;border-radius:14px;margin-bottom:10px"><b>Speak, and it's done.</b><span class="hsub">Gusan lives in your keyboard. Say what you want; it writes it where your cursor is.</span></div>
<div class="group">
<div class="cell"><div class="ic" style="background:var(--ok)">✓</div><div class="t">Sign in with Google</div></div>
<div class="cell"><div class="ic" style="background:#0a84ff">2</div><div class="t">Add the Gusan keyboard<small>Settings › Keyboards · Allow Full Access</small><button class="pillb blue" style="padding:6px 12px;margin-top:8px">Open Settings</button></div></div>
<div class="cell" style="opacity:.5"><div class="ic" style="background:#8e8e93">3</div><div class="t">Allow the microphone</div></div>
<div class="cell" style="opacity:.5"><div class="ic" style="background:#8e8e93">4</div><div class="t">Try it: open Notes, tap the mic</div></div></div>
</div></div>
</div></div><div class="cap"><b>First run</b>One checklist, four steps, straight to Home.</div></div>
</div>

<h2>4. Build order</h2>
<table><tr><th>Phase</th><th>What you get on your iPhone</th><th>Done when</th></tr>
<tr><td><span class="tag">1 · Keyboard + strip</span></td><td>Apple-faithful keyboard with the Gusan strip. Smart by default: mic → speak → mic → text at cursor, chimes, Undo, glowing Dynamic Island while listening. First-tap screen. New logo. App: Home, Settings, first run.</td><td>You dictate a reply in WhatsApp and rewrite a sentence in Notes with zero copy-paste.</td></tr>
<tr><td><span class="tag">2 · Screen + Back Tap</span></td><td>Screen icon in the strip, selected by default (frame at start and at finish). Back Tap: screenshot + listen + do, result copied and announced. Connections: Google Calendar and iPhone Calendar (read-only first), used by Smart Actions.</td><td>"Reply to this" in Gmail works from what's on screen, and "am I free Friday at 3?" is answered from your calendar.</td></tr>
<tr><td><span class="tag">3 · Agent + Sessions</span></td><td>Agent mode in the strip: 3-second card, then Live Activity (Dynamic Island + lock screen) with progress, Copy and Open. Confirm card or notification. Sessions tab (Smart / Agent) with chat, continue, stop, memory.</td><td>You run a test booking and cancel a second one, from the strip.</td></tr>
<tr><td><span class="tag">4 · Vault</span></td><td>Vault tab: add logins from iPhone Passwords (Face ID), "needs a login" card, server-side fill so the agent never sees a password.</td><td>The agent signs in to a test site after your Face ID, and the password never appears in the session log or chat.</td></tr></table>
<p class="sub" style="margin-top:8px">Each phase: builder → critic → me → you on your iPhone. The next phase starts only after you approve the previous one on your phone.</p>


<h2>4b. Logins the agent never sees</h2>
<p>I checked how this is done today. H Company's Agents API has a built-in answer, and it is the right design for Gusan: <b>Vaults</b>. A vault holds one item per login (the sites it applies to, plus username, password, email, one-time-code secret). The agent never receives a secret. When the page it's on matches a stored site, the session offers the agent a single action, <code>fill_secret_at</code>, which types the value into the page <b>outside the agent's context</b>. No endpoint ever returns a secret, and for 2FA the session types the current code, never the secret itself. This is cloud-browser only, which is what Gusan uses.</p>
<table><tr><th>Step</th><th>What happens</th><th>Who sees the password</th></tr>
<tr><td><span class="tag">1 · Add a login</span></td><td>In Vault you tap <b>Add from iPhone Passwords</b>. iOS shows its own password picker; Face ID; iOS fills the login into Gusan's form. Gusan sends it over TLS to Gusan Cloud, which writes it straight into your personal H vault item (one vault per Gusan user, named by your user id) and stores only the site name and username for the list. The plaintext is never written to Gusan's database or logs.</td><td>You, iOS, H's vault (encrypted). Not the agent, not Gusan's database.</td></tr>
<tr><td><span class="tag">2 · Agent hits a login</span></td><td>Sessions start <b>without</b> the vault. When the agent reaches a sign-in page it stops and reports "needs a login for airfrance.com". Gusan Cloud marks the task <code>awaiting_login</code> and you get the card / notification.</td><td>Nobody.</td></tr>
<tr><td><span class="tag">3 · You approve</span></td><td>Face ID on your phone. Gusan Cloud then continues the session with the vault bound <b>for that site only</b>, for that session only. H's domain gating does the rest: the item is eligible only on that site or its subdomains.</td><td>H types it into the page. The agent only sees "filled".</td></tr>
<tr><td><span class="tag">4 · Afterwards</span></td><td>The binding ends with the session. Gusan Cloud records "login used: Air France, 09:41, session X" (no values). Remove a login in Vault and it's deleted from the H vault too.</td><td>Audit entry only.</td></tr></table>
<div class="note" style="margin-top:12px"><b>Rules that hold everywhere:</b> secrets never appear in any prompt, log, screenshot caption, chat message or notification; the keyboard extension never handles a password (only the app does, after Face ID); Gusan Cloud talks to H with the server-side H key and never returns vault contents to a client; Privacy Mode disables the Agent entirely; no payment cards in v1 (the agent asks you to pay yourself in the confirm step).<br><br><b>Limits to know:</b> iOS never lets Gusan read all your passwords at once, so each site is added once by you, with one tap + Face ID. Sites that sign in with Google/Apple or a code sent by SMS need you to finish that step yourself in the session chat (H can take over the one-time code only if you add the authenticator secret). Later option: connect a 1Password account instead of the H vault; H supports it natively and 1Password has an "Agentic Autofill" approval flow in iOS apps.</div>
<h2>5. Decisions for you</h2>
<ol>
<li><b>Keyboard look:</b> an exact copy of Apple's keyboard (recommended), or Gusan's own style?</li>
<li><b>Screen context on by default:</b> this means Gusan Screen runs in the background while you use the phone, and iOS shows its red recording indicator the whole time. The strip icon only decides whether the frames are used. OK with the permanent indicator, or should Gusan Screen start only when you first select the icon?</li>
<li><b>Vault:</b> H vault per user, Face ID before every login, no payment cards in v1 (section 4b)?</li>
<li><b>Back Tap:</b> in phase 2, or later?</li>
<li><b>Connections:</b> start with Google Calendar + iPhone Calendar, read-only, in phase 2? Later: Contacts, Gmail, and letting the Agent create events (write access) after your Confirm.</li>
</ol>
<p class="sub">Known limits that stay: a keyboard cannot take a screenshot by itself, so screen context needs Gusan Screen running (red indicator); the first mic tap per session opens Gusan for a moment (iOS rule); the glowing Dynamic Island is a Live Activity that Gusan starts during that first moment and keeps alive (quiet, just the logo) while it stays ready in the background, lighting up on each recording — iOS does not let an app start one from the background, so if iOS has closed Gusan, the hop happens again and restarts it; the agent's cloud browser starts with no logins; a free Apple ID means builds expire after 7 days and notifications arrive only when the app checks, not instantly.</p>
</div></body></html>
