"""Collect clean pieces of the person's own voice for "My voice" -> "From my meetings".

Where the voice comes from: a meeting recorded from both sides keeps the microphone on its own
track (audio_partN_mic.wav) next to the call's sound (audio_partN_system.wav). Only calls are
used: a part counts only when the call carried the other side's voice through most of its
minutes, and only those minutes count (live_call). An in-person meeting or a lecture has a
silent call track (or just a short video in it), and then everyone in the room is on the mic.
Inside a call a piece is kept only when the mic is clearly speaking AND the call is quiet around
it (nobody else talking, no speaker bleed of other people into the mic). Dictations keep no
audio, so they can't be used.

The person can always choose "Record now" instead (READ_TEXTS + a consent phrase).
Pure numpy, no Qt. Tracks are read a few minutes at a time (a long meeting never sits in memory
whole, and a stop is seen within a block); callers still run it on a worker thread.
"""
import io
import random
import wave
from pathlib import Path

import numpy as np

import meeting_store

RATE = 16000
FRAME = int(0.03 * RATE)              # 30 ms frames
ENOUGH_S = 60.0                       # ElevenLabs instant clones want about 1-2 minutes
TARGET_S = 150.0                      # stay under the server's 200 s / 12 MB limits
MAX_CLIPS = 25
# ElevenLabs refuses any sample shorter than 4.6 s ("audio_too_short"), so short bits of the
# person's speech (>= MIN_PIECE_S each) are joined, with a short pause between them, into samples
# of SAMPLE_S (at least MIN_SAMPLE_S, at most about MAX_CLIP_S).
MIN_PIECE_S = 1.0
MIN_SAMPLE_S = 6.0
SAMPLE_S = 10.0
MAX_CLIP_S = 15.0
JOIN_PAUSE_S = 0.3
GAP_S = 0.4                           # a pause shorter than this stays inside a clip
OTHER_GUARD_S = 0.3                   # keep this far from anyone else's voice
LIVE_WINDOW_S = 60.0                  # a part's call is judged a minute at a time:
LIVE_CALL_S = 3.0                     # a minute with this much of the other side's voice is a live call,
LIVE_SHARE = 0.6                      # and a part is used only when most of its minutes are
BLOCK_S = 300.0                       # tracks are read 5 minutes at a time


# ── reading ──────────────────────────────────────────────────────────────────────────────────
def read_wav(path, max_seconds=None):
    """A WAV file as 16 kHz mono int16 (other rates/channels converted)."""
    with wave.open(str(path), "rb") as w:
        ch, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        n = w.getnframes() if max_seconds is None else min(w.getnframes(), int(max_seconds * rate))
        raw = w.readframes(n)
    if width != 2:
        return np.zeros(0, np.int16)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        a = a[: len(a) - len(a) % ch].reshape(-1, ch).mean(axis=1)
    if rate != RATE and len(a):
        m = int(round(len(a) * RATE / rate))
        a = np.interp(np.linspace(0, len(a) - 1, m), np.arange(len(a)), a)
    return a.astype(np.int16)


def to_wav_bytes(a):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(a, np.int16).tobytes())
    return buf.getvalue()


# ── finding own-voice regions ────────────────────────────────────────────────────────────────
def frame_rms(a):
    n = len(a) // FRAME
    if n == 0:
        return np.zeros(0, np.float32)
    x = a[: n * FRAME].astype(np.float32).reshape(n, FRAME)
    return np.sqrt((x * x).mean(axis=1))


def _active(rms):
    if not len(rms):
        return np.zeros(0, bool)
    # The noise floor comes from the quietest frames; when there is hardly any silence (a long
    # monologue) that "floor" is speech itself, so the bar is also capped by the loud frames.
    floor = max(30.0, float(np.percentile(rms, 5)))
    loud = float(np.percentile(rms, 95))
    return rms > max(300.0, min(4.0 * floor, 0.35 * loud))


def _dilate(mask, frames):
    if frames <= 0 or not mask.any():
        return mask
    k = np.ones(2 * frames + 1, dtype=np.int32)
    return np.convolve(mask.astype(np.int32), k, mode="same") > 0


def own_voice_regions(mic, system=None):
    """[(start_sample, end_sample)] where only the person is speaking."""
    has_system = system is not None and len(system)
    return _regions(frame_rms(mic), frame_rms(system) if has_system else None)


def _regions(mic_rms, sys_rms=None):
    """own_voice_regions from the two tracks' frame loudness (frame_rms)."""
    mic_on = _active(mic_rms)
    if sys_rms is not None and len(sys_rms):
        n = min(len(mic_on), len(sys_rms))
        mic_on = mic_on[:n]
        other = _dilate(_active(sys_rms[:n]), int(OTHER_GUARD_S * RATE / FRAME))
        mic_on = mic_on & ~other
    regions = []
    gap = int(GAP_S * RATE / FRAME)
    start = None
    last_on = None
    for i, on in enumerate(mic_on):
        if on:
            if start is None:
                start = i
            last_on = i
        elif start is not None and i - last_on > gap:
            regions.append((start, last_on + 1))
            start = None
    if start is not None:
        regions.append((start, last_on + 1))
    out = []
    lo, hi = int(MIN_PIECE_S * RATE / FRAME), int(MAX_CLIP_S * RATE / FRAME)
    for s, e in regions:
        if e - s < lo:
            continue
        # long talk is cut into equal parts of at most MAX_CLIP_S (no tiny last part)
        n = -(-(e - s) // hi)
        step = (e - s) / n
        for k in range(n):
            a, b = s + int(round(k * step)), s + int(round((k + 1) * step))
            out.append((a * FRAME, b * FRAME))
    return out


def live_call(sys_rms):
    """Per 30 ms frame of a part: True inside the minutes where the call carried the other side's
    voice. None when most of its minutes had none (an in-person meeting or a lecture: the call
    track is silent, or holds only a short video, and everyone in the room is on the mic - none of
    it may be taken as the person's own voice)."""
    if sys_rms is None or not len(sys_rms):
        return None
    on = _active(sys_rms)
    n = len(on)
    k = max(1, int(round(n / (LIVE_WINDOW_S * RATE / FRAME))))
    need = int(LIVE_CALL_S * RATE / FRAME)
    edges = np.linspace(0, n, k + 1).astype(int)
    live = np.zeros(n, bool)
    minutes = 0
    for a, b in zip(edges[:-1], edges[1:]):
        if int(on[a:b].sum()) >= need:
            live[a:b] = True
            minutes += 1
    return live if minutes >= LIVE_SHARE * k else None


def _plain(w):
    """A track the way the recorder writes it (16 kHz mono 16-bit): read in place, block by block."""
    return (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, RATE, 2)


def track_rms(path, should_stop=lambda: False):
    """frame_rms of a whole track, read BLOCK_S at a time (None when stopped)."""
    with wave.open(str(path), "rb") as w:
        if _plain(w):
            step = max(FRAME, int(BLOCK_S * RATE) // FRAME * FRAME)     # whole frames per block
            parts = []
            while True:
                if should_stop():
                    return None
                raw = w.readframes(step)
                if not raw:
                    break
                parts.append(frame_rms(np.frombuffer(raw, dtype=np.int16)))
            return np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return None if should_stop() else frame_rms(read_wav(path))           # another format (rare)


def _pieces(path, regions):
    """The track's samples for each (start, end) region, read one region at a time."""
    with wave.open(str(path), "rb") as w:
        if _plain(w):
            for s, e in regions:
                w.setpos(s)
                yield np.frombuffer(w.readframes(e - s), dtype=np.int16)
            return
    a = read_wav(path)
    for s, e in regions:
        yield a[s:e]


def clean_enough(clip):
    """No clipping, and clearly louder than silence."""
    if not len(clip):
        return False
    if np.mean(np.abs(clip.astype(np.int32)) >= 32000) > 0.001:
        return False
    return float(np.sqrt(np.mean(clip.astype(np.float32) ** 2))) > 400.0


# ── the whole library ────────────────────────────────────────────────────────────────────────
class Found:
    def __init__(self):
        self.clips = []              # (wav bytes, seconds, meeting folder)
        self.seconds = 0.0
        self.meetings = set()

    @property
    def enough(self):
        return self.seconds >= ENOUGH_S

    def summary(self):
        return {"seconds": round(self.seconds), "meetings": len(self.meetings), "enough": self.enough,
                "clips": len(self.clips)}


def meeting_folders(root=None, limit=60):
    root = Path(root) if root else meeting_store.meetings_dir()
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(folders, key=lambda p: p.name, reverse=True)[:limit]       # newest first


def find_own_voice(folders=None, target_s=TARGET_S, max_clips=MAX_CLIPS, should_stop=lambda: False):
    """Walk the meetings (newest first) and collect up to ``target_s`` seconds of clean samples,
    each MIN_SAMPLE_S long at least (short bits are joined; a last bit too short is left out).
    Calls only (live_call). ``should_stop`` is asked between the blocks of every track read."""
    found = Found()
    pending = []                     # (pcm, folder) pieces not yet long enough for a sample
    pause = np.zeros(int(JOIN_PAUSE_S * RATE), np.int16)

    def pending_s():
        return sum(len(p) for p, _f in pending) / RATE + JOIN_PAUSE_S * max(0, len(pending) - 1)

    def flush():
        parts = []
        for i, (p, _f) in enumerate(pending):
            if i:
                parts.append(pause)
            parts.append(p)
        sample = np.concatenate(parts)
        secs = len(sample) / RATE
        found.clips.append((to_wav_bytes(sample), secs, pending[0][1]))
        found.seconds += secs
        found.meetings.update(f for _p, f in pending)
        pending.clear()

    def full():
        return found.seconds >= target_s or len(found.clips) >= max_clips

    def take(clip, folder):
        if pending and pending_s() + JOIN_PAUSE_S + len(clip) / RATE > MAX_CLIP_S \
                and pending_s() >= MIN_SAMPLE_S:
            flush()
            if full():
                return
        pending.append((clip, folder))
        if pending_s() >= SAMPLE_S:
            flush()

    for folder in (folders if folders is not None else meeting_folders()):
        if should_stop() or full():
            break
        for _part, mic_path, sys_path in meeting_store.audio_tracks(folder):
            if should_stop() or full():
                break
            if mic_path is None or sys_path is None:
                continue                         # no call track: not known to be a call
            try:
                sys_rms = track_rms(sys_path, should_stop)
                live = live_call(sys_rms)
                if live is None:
                    continue                     # in person (or a lecture): not only the person on the mic
                mic_rms = track_rms(mic_path, should_stop)
                if mic_rms is None:
                    break                        # stopped
                regions = [(s, e) for s, e in _regions(mic_rms, sys_rms) if live[s // FRAME:e // FRAME].all()]
                for clip in _pieces(mic_path, regions):
                    if clean_enough(clip):
                        take(clip, str(folder))
                        if full():
                            break
            except (OSError, wave.Error, EOFError):
                continue
    if pending and not full() and pending_s() >= MIN_SAMPLE_S:
        flush()
    return found


# ── "Record now" ─────────────────────────────────────────────────────────────────────────────
# Three short texts in each picker language, about 20 s each read aloud: with the consent words
# that is about a minute, what ElevenLabs wants for an instant clone.
READ_TEXTS = {
    "en": [
        "The meeting moved to Thursday, so we have two more days to finish the slides. I will write the first "
        "part tonight and send it to you in the morning. Please add your notes when you can, and tell me if "
        "anything looks wrong.",
        "Could you send me the numbers before lunch? I want to check them with the team this afternoon, before "
        "the call with our partners. If something is missing, just call me, and we can look at it together. It "
        "should not take more than ten minutes.",
        "Thanks, that was really helpful. I think the plan is clear now, and everyone knows what to do next. "
        "Let's talk again next week and see how it went. Have a good evening, and say hello to your family for "
        "me.",
    ],
    "fr": [
        "La réunion a été déplacée à jeudi, nous avons donc deux jours de plus pour finir les diapositives. "
        "J'écrirai la première partie ce soir et je te l'enverrai demain matin. Ajoute tes remarques quand tu "
        "peux, et dis-moi si quelque chose ne va pas.",
        "Tu peux m'envoyer les chiffres avant midi ? Je veux les vérifier avec l'équipe cet après-midi, avant "
        "l'appel avec nos partenaires. S'il manque quelque chose, appelle-moi, et on regardera ça ensemble. Ça "
        "ne devrait pas prendre plus de dix minutes.",
        "Merci, c'était vraiment utile. Je pense que le plan est clair maintenant, et chacun sait ce qu'il doit "
        "faire. On en reparle la semaine prochaine pour voir comment ça s'est passé. Bonne soirée, et salue ta "
        "famille de ma part.",
    ],
    "hy": [
        "Հանդիպումը տեղափոխվեց հինգշաբթի, ուրեմն սլայդներն ավարտելու համար ևս երկու օր ունենք։ Առաջին մասը կգրեմ "
        "այս երեկո և վաղն առավոտյան կուղարկեմ քեզ։ Երբ կարողանաս, ավելացրու քո նշումները և ասա, եթե ինչ-որ բան "
        "սխալ է թվում։",
        "Կարո՞ղ ես թվերը ուղարկել մինչև ճաշ։ Ուզում եմ այսօր կեսօրից հետո թիմի հետ ստուգել դրանք՝ մինչև "
        "գործընկերների հետ զանգը։ Եթե ինչ-որ բան պակասում է, պարզապես զանգիր ինձ, և միասին կնայենք։ Տասը րոպեից "
        "ավելի չպետք է տևի։",
        "Շնորհակալություն, շատ օգտակար էր։ Կարծում եմ՝ ծրագիրն այժմ պարզ է, և բոլորը գիտեն՝ ինչ անել հետո։ Եկեք "
        "հաջորդ շաբաթ նորից խոսենք և տեսնենք, թե ինչպես անցավ։ Լավ երեկո, և իմ կողմից բարևիր ընտանիքիդ։",
    ],
    "ru": [
        "Встречу перенесли на четверг, так что у нас есть ещё два дня, чтобы закончить слайды. Я напишу первую "
        "часть сегодня вечером и пришлю её тебе утром. Добавь свои заметки, когда сможешь, и скажи, если что-то "
        "выглядит не так.",
        "Можешь прислать мне цифры до обеда? Я хочу проверить их с командой сегодня днём, до звонка с "
        "партнёрами. Если чего-то не хватает, просто позвони мне, и мы посмотрим вместе. Это займёт не больше "
        "десяти минут.",
        "Спасибо, это было очень полезно. Думаю, теперь план понятен, и все знают, что делать дальше. Давай "
        "поговорим снова на следующей неделе и посмотрим, как всё прошло. Хорошего вечера, и передай привет "
        "своей семье.",
    ],
    "es": [
        "La reunión se movió al jueves, así que tenemos dos días más para terminar las diapositivas. Esta noche "
        "escribiré la primera parte y te la enviaré por la mañana. Añade tus notas cuando puedas y dime si algo "
        "no está bien.",
        "¿Me puedes enviar los números antes del almuerzo? Quiero revisarlos con el equipo esta tarde, antes de "
        "la llamada con nuestros socios. Si falta algo, llámame y lo vemos juntos. No debería llevar más de diez "
        "minutos.",
        "Gracias, fue muy útil. Creo que el plan ya está claro y todos saben qué hacer ahora. Hablemos otra vez "
        "la semana que viene y veamos cómo fue. Que tengas una buena noche, y saluda a tu familia de mi parte.",
    ],
    "de": [
        "Das Meeting wurde auf Donnerstag verschoben, also haben wir zwei Tage mehr für die Folien. Ich schreibe "
        "heute Abend den ersten Teil und schicke ihn dir morgen früh. Füge deine Notizen hinzu, wenn du kannst, "
        "und sag mir, wenn etwas nicht stimmt.",
        "Kannst du mir die Zahlen vor dem Mittagessen schicken? Ich will sie heute Nachmittag mit dem Team "
        "prüfen, vor dem Anruf mit unseren Partnern. Wenn etwas fehlt, ruf mich einfach an, dann schauen wir es "
        "uns zusammen an. Es sollte nicht länger als zehn Minuten dauern.",
        "Danke, das war wirklich hilfreich. Ich glaube, der Plan ist jetzt klar, und alle wissen, was als "
        "Nächstes zu tun ist. Lass uns nächste Woche wieder sprechen und sehen, wie es gelaufen ist. Einen "
        "schönen Abend noch, und grüß deine Familie von mir.",
    ],
    "ar": [
        "تم نقل الاجتماع إلى يوم الخميس، لذلك لدينا يومان إضافيان لإنهاء الشرائح. سأكتب الجزء الأول الليلة "
        "وأرسله إليك في الصباح. أضف ملاحظاتك عندما تستطيع، وأخبرني إذا بدا لك أي شيء غير صحيح.",
        "هل يمكنك أن ترسل لي الأرقام قبل الغداء؟ أريد أن أراجعها مع الفريق بعد الظهر، قبل المكالمة مع شركائنا. "
        "إذا كان هناك شيء ناقص، اتصل بي فقط، وسننظر فيه معًا. لن يستغرق ذلك أكثر من عشر دقائق.",
        "شكرًا، كان ذلك مفيدًا جدًا. أظن أن الخطة واضحة الآن، والجميع يعرف ما عليه فعله بعد ذلك. لنتحدث مرة أخرى "
        "الأسبوع القادم ونرى كيف سارت الأمور. أتمنى لك مساءً سعيدًا، وأبلغ عائلتك تحياتي.",
    ],
    "bn": [
        "মিটিংটা বৃহস্পতিবারে সরানো হয়েছে, তাই স্লাইডগুলো শেষ করার জন্য আমাদের হাতে আরও দুই দিন আছে। আমি আজ "
        "রাতে প্রথম অংশটা লিখব আর কাল সকালে তোমাকে পাঠিয়ে দেব। সময় পেলে তোমার নোটগুলো যোগ করো, আর কিছু ভুল মনে "
        "হলে আমাকে জানিও।",
        "দুপুরের খাবারের আগে কি সংখ্যাগুলো আমাকে পাঠাতে পারবে? আজ বিকেলে, পার্টনারদের সঙ্গে কলের আগে, আমি টিমের "
        "সঙ্গে সেগুলো মিলিয়ে দেখতে চাই। কিছু বাদ পড়লে আমাকে একটা ফোন দিও, আমরা একসঙ্গে দেখে নেব। দশ মিনিটের "
        "বেশি লাগার কথা নয়।",
        "ধন্যবাদ, এটা সত্যিই খুব কাজে লেগেছে। আমার মনে হয় পরিকল্পনাটা এখন পরিষ্কার, আর সবাই জানে এরপর কী করতে "
        "হবে। চলো আগামী সপ্তাহে আবার কথা বলি আর দেখি কেমন গেল। তোমার সন্ধ্যা ভালো কাটুক, আর তোমার পরিবারকে আমার "
        "শুভেচ্ছা জানিও।",
    ],
    "bg": [
        "Срещата се премести за четвъртък, така че имаме още два дни да довършим слайдовете. Тази вечер ще "
        "напиша първата част и ще ти я изпратя сутринта. Добави бележките си, когато можеш, и ми кажи, ако нещо "
        "не изглежда наред.",
        "Можеш ли да ми изпратиш числата преди обяд? Искам да ги проверя с екипа следобед, преди разговора с "
        "нашите партньори. Ако нещо липсва, просто ми се обади и ще го погледнем заедно. Не би трябвало да "
        "отнеме повече от десет минути.",
        "Благодаря, това беше наистина полезно. Мисля, че планът вече е ясен и всеки знае какво да прави "
        "нататък. Да поговорим пак следващата седмица и да видим как е минало. Приятна вечер и поздрави "
        "семейството си от мен.",
    ],
    "zh": [
        "会议改到了星期四，所以我们还有两天时间把幻灯片做完。我今晚写好第一部分，明天早上发给你。你有空的时候加上你的意见，如果有什么地方不对，就告诉我。",
        "你能在午饭前把数据发给我吗？我想今天下午和团队核对一下，赶在和合作伙伴通话之前。如果缺了什么，直接给我打电话，我们一起看看。应该不会超过十分钟。",
        "谢谢，这真的很有帮助。我觉得现在计划已经很清楚了，大家都知道接下来该做什么。我们下周再聊，看看进展如何。祝你晚上愉快，也替我向你的家人问好。",
    ],
    "hr": [
        "Sastanak je pomaknut na četvrtak, pa imamo još dva dana da završimo slajdove. Večeras ću napisati prvi "
        "dio i poslati ti ga ujutro. Dodaj svoje bilješke kad stigneš i reci mi ako ti nešto ne izgleda dobro.",
        "Možeš li mi poslati brojke prije ručka? Želim ih danas poslijepodne provjeriti s timom, prije poziva s "
        "našim partnerima. Ako nešto nedostaje, samo me nazovi pa ćemo to pogledati zajedno. Ne bi trebalo "
        "trajati dulje od deset minuta.",
        "Hvala, to je bilo stvarno korisno. Mislim da je plan sada jasan i da svi znaju što dalje. Čujmo se opet "
        "sljedeći tjedan pa ćemo vidjeti kako je prošlo. Ugodna večer, i pozdravi obitelj od mene.",
    ],
    "cs": [
        "Schůzka se přesunula na čtvrtek, takže máme ještě dva dny na dokončení slajdů. Dnes večer napíšu první "
        "část a ráno ti ji pošlu. Přidej své poznámky, až budeš mít čas, a řekni mi, kdyby ti něco nesedělo.",
        "Můžeš mi poslat ta čísla před obědem? Chci je dnes odpoledne projít s týmem, ještě před hovorem s "
        "našimi partnery. Kdyby něco chybělo, prostě mi zavolej a podíváme se na to spolu. Nemělo by to trvat "
        "déle než deset minut.",
        "Díky, to mi opravdu pomohlo. Myslím, že plán je teď jasný a všichni vědí, co dělat dál. Ozvěme se zase "
        "příští týden a uvidíme, jak to dopadlo. Hezký večer a pozdravuj ode mě rodinu.",
    ],
    "da": [
        "Mødet er flyttet til torsdag, så vi har to dage mere til at gøre slides færdige. Jeg skriver den første "
        "del i aften og sender den til dig i morgen tidlig. Tilføj dine noter, når du kan, og sig til, hvis "
        "noget ser forkert ud.",
        "Kan du sende mig tallene før frokost? Jeg vil gerne gennemgå dem med teamet i eftermiddag, før opkaldet "
        "med vores partnere. Hvis der mangler noget, så ring bare til mig, så kigger vi på det sammen. Det burde "
        "ikke tage mere end ti minutter.",
        "Tak, det var virkelig en stor hjælp. Jeg tror, planen er klar nu, og alle ved, hvad de skal gøre. Lad "
        "os tale sammen igen i næste uge og se, hvordan det gik. Hav en god aften, og hils din familie fra mig.",
    ],
    "nl": [
        "De vergadering is verplaatst naar donderdag, dus we hebben nog twee dagen om de slides af te maken. Ik "
        "schrijf vanavond het eerste deel en stuur het je morgenochtend. Voeg je opmerkingen toe wanneer je "
        "kunt, en laat het me weten als er iets niet klopt.",
        "Kun je me de cijfers vóór de lunch sturen? Ik wil ze vanmiddag met het team doornemen, vóór het gesprek "
        "met onze partners. Als er iets ontbreekt, bel me gewoon, dan kijken we er samen naar. Het zou niet "
        "langer dan tien minuten moeten duren.",
        "Bedankt, dat was echt nuttig. Ik denk dat het plan nu duidelijk is en dat iedereen weet wat er moet "
        "gebeuren. Laten we volgende week weer praten en kijken hoe het is gegaan. Fijne avond nog, en doe je "
        "familie de groeten van me.",
    ],
    "fi": [
        "Kokous siirtyi torstaille, joten meillä on kaksi päivää lisää aikaa saada diat valmiiksi. Kirjoitan "
        "ensimmäisen osan tänä iltana ja lähetän sen sinulle aamulla. Lisää omat huomiosi, kun ehdit, ja kerro "
        "minulle, jos jokin näyttää väärältä.",
        "Voisitko lähettää minulle luvut ennen lounasta? Haluan käydä ne läpi tiimin kanssa tänään iltapäivällä, "
        "ennen puhelua kumppaneidemme kanssa. Jos jotain puuttuu, soita minulle, niin katsotaan sitä yhdessä. "
        "Siihen ei pitäisi mennä kymmentä minuuttia kauempaa.",
        "Kiitos, tästä oli todella paljon apua. Luulen, että suunnitelma on nyt selvä ja kaikki tietävät, mitä "
        "seuraavaksi tehdään. Puhutaan taas ensi viikolla ja katsotaan, miten meni. Mukavaa iltaa, ja kerro "
        "terveisiä perheellesi.",
    ],
    "ka": [
        "შეხვედრა ხუთშაბათისთვის გადაიტანეს, ასე რომ, სლაიდების დასასრულებლად კიდევ ორი დღე გვაქვს. პირველ "
        "ნაწილს დღეს საღამოს დავწერ და დილით გამოგიგზავნი. როცა შეძლებ, შენი შენიშვნები დაამატე და მითხარი, თუ "
        "რამე არასწორად გეჩვენება.",
        "შეგიძლია ციფრები სადილამდე გამომიგზავნო? მინდა, დღეს ნაშუადღევს გუნდთან ერთად გადავამოწმო, "
        "პარტნიორებთან ზარამდე. თუ რამე აკლია, უბრალოდ დამირეკე და ერთად შევხედავთ. ათ წუთზე მეტს არ უნდა "
        "დასჭირდეს.",
        "გმადლობ, ეს ძალიან გამოსადეგი იყო. მგონი, გეგმა ახლა ნათელია და ყველამ იცის, რა უნდა გააკეთოს შემდეგ. "
        "მომავალ კვირას ისევ ვისაუბროთ და ვნახოთ, როგორ ჩაიარა. სასიამოვნო საღამოს გისურვებ და ჩემგან ოჯახს "
        "მოკითხვა გადაეცი.",
    ],
    "el": [
        "Η συνάντηση μετατέθηκε για την Πέμπτη, οπότε έχουμε δύο μέρες ακόμα για να τελειώσουμε τις διαφάνειες. "
        "Θα γράψω το πρώτο μέρος απόψε και θα σου το στείλω το πρωί. Πρόσθεσε τις σημειώσεις σου όταν μπορείς "
        "και πες μου αν κάτι δεν σου φαίνεται σωστό.",
        "Μπορείς να μου στείλεις τα νούμερα πριν από το μεσημεριανό; Θέλω να τα ελέγξω με την ομάδα σήμερα το "
        "απόγευμα, πριν από την κλήση με τους συνεργάτες μας. Αν λείπει κάτι, απλώς πάρε με τηλέφωνο και θα το "
        "δούμε μαζί. Δεν θα πάρει πάνω από δέκα λεπτά.",
        "Ευχαριστώ, αυτό ήταν πραγματικά χρήσιμο. Νομίζω ότι το σχέδιο είναι πια ξεκάθαρο και όλοι ξέρουν τι "
        "πρέπει να κάνουν. Ας τα ξαναπούμε την επόμενη εβδομάδα να δούμε πώς πήγε. Καλό βράδυ, και χαιρετισμούς "
        "στην οικογένειά σου.",
    ],
    "he": [
        "הפגישה הועברה ליום חמישי, אז יש לנו עוד יומיים לסיים את השקפים. אכתוב את החלק הראשון הערב ואשלח לך אותו "
        "בבוקר. תוסיף את ההערות שלך כשתוכל, ותגיד לי אם משהו נראה לא בסדר.",
        "אתה יכול לשלוח לי את המספרים לפני ארוחת הצהריים? אני רוצה לבדוק אותם עם הצוות אחר הצהריים, לפני השיחה "
        "עם השותפים שלנו. אם חסר משהו, פשוט תתקשר אליי ונסתכל על זה ביחד. זה לא אמור לקחת יותר מעשר דקות.",
        "תודה, זה באמת עזר מאוד. נראה לי שהתוכנית ברורה עכשיו, וכולם יודעים מה לעשות הלאה. בוא נדבר שוב בשבוע "
        "הבא ונראה איך זה הלך. ערב טוב, ותמסור דרישת שלום למשפחה שלך.",
    ],
    "hi": [
        "मीटिंग अब गुरुवार को है, इसलिए स्लाइड पूरी करने के लिए हमारे पास दो दिन और हैं। मेरा इरादा है कि आज रात "
        "पहला हिस्सा लिखूँ और सुबह तुम्हें भेज दूँ। जब समय मिले, अपने नोट्स जोड़ देना, और अगर कुछ गलत लगे तो "
        "मुझे बताना।",
        "क्या तुम लंच से पहले मुझे आँकड़े भेज सकते हो? आज दोपहर, पार्टनर्स के साथ कॉल से पहले, मुझे इन्हें टीम "
        "के साथ जाँचना है। अगर कुछ छूट गया हो, तो बस मुझे फ़ोन कर देना, हम साथ में देख लेंगे। इसमें दस मिनट से "
        "ज़्यादा नहीं लगना चाहिए।",
        "शुक्रिया, यह सच में बहुत काम आया। मुझे लगता है कि अब योजना साफ़ है, और सबको पता है कि आगे क्या करना है। "
        "चलो अगले हफ़्ते फिर बात करते हैं और देखते हैं कि कैसा रहा। तुम्हारी शाम अच्छी गुज़रे, और अपने परिवार को "
        "मेरी ओर से नमस्ते कहना।",
    ],
    "hu": [
        "A megbeszélést csütörtökre tették át, így van még két napunk, hogy befejezzük a diákat. Ma este megírom "
        "az első részt, és reggel elküldöm neked. Tedd hozzá a megjegyzéseidet, amikor tudod, és szólj, ha "
        "valami nem tűnik jónak.",
        "El tudnád küldeni a számokat ebéd előtt? Ma délután át szeretném nézni őket a csapattal, még a "
        "partnereinkkel való hívás előtt. Ha valami hiányzik, csak hívj fel, és megnézzük együtt. Nem kellene "
        "tíz percnél tovább tartania.",
        "Köszönöm, ez nagyon hasznos volt. Szerintem most már világos a terv, és mindenki tudja, mi a következő "
        "lépés. Beszéljünk újra jövő héten, és nézzük meg, hogyan sikerült. Szép estét, és add át üdvözletemet a "
        "családodnak.",
    ],
    "id": [
        "Rapatnya dipindah ke hari Kamis, jadi kita punya dua hari lagi untuk menyelesaikan slide. Saya akan "
        "menulis bagian pertama malam ini dan mengirimkannya kepadamu besok pagi. Tolong tambahkan catatanmu "
        "kalau sempat, dan beri tahu saya kalau ada yang kelihatan salah.",
        "Bisakah kamu mengirim angka-angkanya sebelum makan siang? Saya ingin mengeceknya bersama tim sore ini, "
        "sebelum panggilan dengan mitra kita. Kalau ada yang kurang, telepon saja saya, nanti kita lihat "
        "bersama. Seharusnya tidak lebih dari sepuluh menit.",
        "Terima kasih, itu sangat membantu. Saya rasa rencananya sekarang sudah jelas, dan semua orang tahu apa "
        "yang harus dilakukan selanjutnya. Ayo kita bicara lagi minggu depan dan lihat bagaimana hasilnya. "
        "Selamat malam, dan sampaikan salam saya untuk keluargamu.",
    ],
    "it": [
        "La riunione è stata spostata a giovedì, quindi abbiamo due giorni in più per finire le slide. Stasera "
        "scriverò la prima parte e te la manderò domattina. Aggiungi le tue note quando puoi, e dimmi se "
        "qualcosa non va.",
        "Puoi mandarmi i numeri prima di pranzo? Voglio controllarli con il team oggi pomeriggio, prima della "
        "chiamata con i nostri partner. Se manca qualcosa, chiamami e lo guardiamo insieme. Non dovrebbe volerci "
        "più di dieci minuti.",
        "Grazie, è stato davvero utile. Penso che ora il piano sia chiaro e che tutti sappiano cosa fare. "
        "Risentiamoci la settimana prossima per vedere com'è andata. Buona serata, e saluta la tua famiglia da "
        "parte mia.",
    ],
    "ja": [
        "会議が木曜日に変更になったので、スライドを仕上げる時間があと二日あります。今夜、最初の部分を書いて、明日の朝に送ります。時間があるときにメモを追加して、何かおかしいところがあれば教えてください。",
        "お昼までに数字を送ってもらえますか？今日の午後、パートナーとの電話の前に、チームと一緒に確認したいんです。何か足りないものがあれば、電話をください。一緒に見ましょう。十分もかからないはずです。",
        "ありがとうございます。本当に助かりました。これで計画ははっきりしたし、みんな次に何をすればいいか分かっていると思います。来週また話して、どうだったか確認しましょう。良い夜を。ご家族にもよろしくお伝えください。",
    ],
    "kk": [
        "Кездесу бейсенбіге ауыстырылды, сондықтан слайдтарды аяқтауға тағы екі күніміз бар. Бүгін кешке бірінші "
        "бөлімін жазып, ертең таңертең саған жіберемін. Уақытың болғанда өз ескертпелеріңді қос, егер бір нәрсе "
        "дұрыс емес сияқты болса, маған айт.",
        "Сандарды түскі асқа дейін маған жібере аласың ба? Бүгін түстен кейін, серіктестерімізбен қоңырауға "
        "дейін, оларды командамен бірге тексергім келеді. Егер бірдеңе жетіспесе, маған жай ғана қоңырау шал, "
        "бірге қараймыз. Бұл он минуттан аспауы керек.",
        "Рақмет, бұл шынымен көп көмектесті. Менің ойымша, жоспар енді түсінікті, және бәрі әрі қарай не істеу "
        "керегін біледі. Келесі аптада тағы сөйлесейік те, қалай өткенін көрейік. Кешің жақсы өтсін, отбасыңа "
        "менен сәлем айт.",
    ],
    "ko": [
        "회의가 목요일로 옮겨져서 슬라이드를 끝낼 시간이 이틀 더 생겼어요. 오늘 밤에 첫 부분을 써서 내일 아침에 보내 드릴게요. 시간 될 때 메모를 추가해 주시고, 이상한 부분이 있으면 알려 "
        "주세요.",
        "점심 전에 숫자를 보내 주실 수 있나요? 오늘 오후에 파트너들과 통화하기 전에 팀과 함께 확인하고 싶어요. 빠진 게 있으면 그냥 전화 주세요. 같이 보면 돼요. 십 분도 안 걸릴 거예요.",
        "고마워요, 정말 도움이 많이 됐어요. 이제 계획이 분명해졌고, 다들 다음에 뭘 해야 할지 아는 것 같아요. 다음 주에 다시 이야기하면서 어떻게 됐는지 봐요. 좋은 저녁 보내시고, "
        "가족분들께도 안부 전해 주세요.",
    ],
    "lv": [
        "Sanāksme pārcelta uz ceturtdienu, tāpēc mums ir vēl divas dienas, lai pabeigtu slaidus. Šovakar "
        "uzrakstīšu pirmo daļu un no rīta to tev nosūtīšu. Pievieno savas piezīmes, kad vari, un pasaki man, ja "
        "kaut kas neizskatās pareizi.",
        "Vai vari man atsūtīt skaitļus pirms pusdienām? Es gribu tos šopēcpusdien pārbaudīt kopā ar komandu, "
        "pirms zvana ar mūsu partneriem. Ja kaut kā trūkst, vienkārši piezvani man, un mēs to apskatīsim kopā. "
        "Tam nevajadzētu aizņemt vairāk par desmit minūtēm.",
        "Paldies, tas tiešām ļoti palīdzēja. Man šķiet, ka plāns tagad ir skaidrs un visi zina, ko darīt tālāk. "
        "Parunāsim atkal nākamnedēļ un paskatīsimies, kā gāja. Jauku vakaru, un pasveicini no manis savu ģimeni.",
    ],
    "lt": [
        "Susitikimas perkeltas į ketvirtadienį, todėl turime dar dvi dienas skaidrėms užbaigti. Šįvakar "
        "parašysiu pirmąją dalį ir rytoj ryte tau ją atsiųsiu. Pridėk savo pastabas, kai galėsi, ir pasakyk, jei "
        "kas nors atrodo ne taip.",
        "Ar galėtum atsiųsti man skaičius prieš pietus? Noriu juos šiandien po pietų patikrinti su komanda, "
        "prieš skambutį su mūsų partneriais. Jei ko nors trūksta, tiesiog paskambink man, ir pažiūrėsime kartu. "
        "Tai neturėtų užtrukti ilgiau nei dešimt minučių.",
        "Ačiū, tai tikrai labai padėjo. Manau, dabar planas aiškus, ir visi žino, ką daryti toliau. "
        "Pasikalbėkime vėl kitą savaitę ir pažiūrėkime, kaip sekėsi. Gero vakaro, ir perduok linkėjimų savo "
        "šeimai.",
    ],
    "ms": [
        "Mesyuarat telah dipindahkan ke hari Khamis, jadi kita ada dua hari lagi untuk menyiapkan slaid. Saya "
        "akan menulis bahagian pertama malam ini dan menghantarnya kepada awak esok pagi. Tolong tambah nota "
        "awak bila sempat, dan beritahu saya kalau ada apa-apa yang nampak salah.",
        "Boleh awak hantar angka-angka itu sebelum makan tengah hari? Saya mahu menyemaknya dengan pasukan "
        "petang ini, sebelum panggilan dengan rakan kongsi kita. Kalau ada yang tertinggal, telefon saja saya, "
        "dan kita tengok bersama-sama. Ia tidak sepatutnya mengambil masa lebih daripada sepuluh minit.",
        "Terima kasih, itu sangat membantu. Saya rasa rancangannya sudah jelas sekarang, dan semua orang tahu "
        "apa yang perlu dibuat seterusnya. Mari kita berbincang lagi minggu depan dan lihat bagaimana hasilnya. "
        "Semoga malam awak menyenangkan, dan kirim salam saya kepada keluarga awak.",
    ],
    "no": [
        "Møtet er flyttet til torsdag, så vi har to dager ekstra til å gjøre ferdig lysbildene. Jeg skriver den "
        "første delen i kveld og sender den til deg i morgen tidlig. Legg til notatene dine når du har tid, og "
        "si fra hvis noe ser feil ut.",
        "Kan du sende meg tallene før lunsj? Jeg vil gå gjennom dem med teamet i ettermiddag, før samtalen med "
        "partnerne våre. Hvis noe mangler, er det bare å ringe meg, så ser vi på det sammen. Det burde ikke ta "
        "mer enn ti minutter.",
        "Takk, det var virkelig nyttig. Jeg tror planen er klar nå, og alle vet hva de skal gjøre videre. La oss "
        "snakkes igjen neste uke og se hvordan det gikk. Ha en fin kveld, og hils familien din fra meg.",
    ],
    "fa": [
        "جلسه به پنجشنبه منتقل شد، پس دو روز دیگر وقت داریم تا اسلایدها را تمام کنیم. امشب بخش اول را می‌نویسم و "
        "صبح برایت می‌فرستم. هر وقت توانستی یادداشت‌هایت را اضافه کن و اگر چیزی به نظرت درست نبود، به من بگو.",
        "می‌توانی قبل از ناهار عددها را برایم بفرستی؟ می‌خواهم امروز بعدازظهر، قبل از تماس با شرکایمان، آن‌ها را "
        "با تیم بررسی کنم. اگر چیزی کم است، فقط به من زنگ بزن تا با هم نگاهش کنیم. نباید بیشتر از ده دقیقه طول "
        "بکشد.",
        "ممنون، واقعاً خیلی کمک کرد. فکر می‌کنم حالا برنامه روشن است و همه می‌دانند قدم بعدی چیست. بیا هفته‌ی "
        "بعد دوباره صحبت کنیم و ببینیم چطور پیش رفت. شب خوبی داشته باشی، و سلام من را به خانواده‌ات برسان.",
    ],
    "pl": [
        "Spotkanie przeniesiono na czwartek, więc mamy jeszcze dwa dni na dokończenie slajdów. Dziś wieczorem "
        "napiszę pierwszą część i rano ci ją wyślę. Dodaj swoje uwagi, kiedy będziesz mieć czas, i daj mi znać, "
        "jeśli coś wygląda źle.",
        "Możesz mi wysłać liczby przed obiadem? Chcę je dziś po południu sprawdzić z zespołem, jeszcze przed "
        "rozmową z naszymi partnerami. Jeśli czegoś brakuje, po prostu do mnie zadzwoń, to razem na to "
        "spojrzymy. Nie powinno to zająć więcej niż dziesięć minut.",
        "Dzięki, to było naprawdę pomocne. Myślę, że plan jest teraz jasny i wszyscy wiedzą, co robić dalej. "
        "Porozmawiajmy znowu w przyszłym tygodniu i zobaczmy, jak poszło. Miłego wieczoru i pozdrów ode mnie "
        "rodzinę.",
    ],
    "pt": [
        "A reunião passou para quinta-feira, então temos mais dois dias para terminar os slides. Vou escrever a "
        "primeira parte hoje à noite e te envio de manhã. Acrescente suas notas quando puder e me diga se algo "
        "parecer errado.",
        "Você pode me mandar os números antes do almoço? Quero conferir com a equipe hoje à tarde, antes da "
        "ligação com nossos parceiros. Se faltar alguma coisa, é só me ligar e a gente vê junto. Não deve levar "
        "mais de dez minutos.",
        "Agradeço, isso ajudou muito. Acho que o plano agora está claro e todo mundo sabe o que fazer. Vamos "
        "conversar de novo na semana que vem para ver como foi. Tenha uma boa noite, e mande um abraço para a "
        "sua família.",
    ],
    "ro": [
        "Ședința s-a mutat joi, așa că mai avem două zile ca să terminăm slide-urile. Diseară scriu prima parte "
        "și ți-o trimit dimineață. Adaugă-ți notițele când poți și spune-mi dacă ceva nu arată bine.",
        "Poți să-mi trimiți cifrele înainte de prânz? Vreau să le verific cu echipa în după-amiaza asta, înainte "
        "de apelul cu partenerii noștri. Dacă lipsește ceva, sună-mă și ne uităm împreună. N-ar trebui să dureze "
        "mai mult de zece minute.",
        "Mulțumesc, chiar m-a ajutat mult. Cred că planul e clar acum și toată lumea știe ce are de făcut mai "
        "departe. Hai să vorbim din nou săptămâna viitoare și să vedem cum a mers. Seară frumoasă și salută-ți "
        "familia din partea mea.",
    ],
    "sr": [
        "Састанак је померен за четвртак, па имамо још два дана да завршимо слајдове. Вечерас ћу написати први "
        "део и ујутру ћу ти га послати. Додај своје белешке кад стигнеш и реци ми ако ти нешто не изгледа добро.",
        "Можеш ли да ми пошаљеш бројеве пре ручка? Хоћу да их данас поподне проверим са тимом, пре позива са "
        "нашим партнерима. Ако нешто недостаје, само ме позови па ћемо то заједно погледати. Не би требало да "
        "траје дуже од десет минута.",
        "Хвала, то је стварно много помогло. Мислим да је план сада јасан и да сви знају шта даље. Чујемо се "
        "поново следеће недеље да видимо како је прошло. Пријатно вече, и поздрави породицу од мене.",
    ],
    "sk": [
        "Stretnutie sa presunulo na štvrtok, takže máme ešte dva dni na dokončenie slajdov. Dnes večer napíšem "
        "prvú časť a ráno ti ju pošlem. Pridaj svoje poznámky, keď budeš mať čas, a povedz mi, ak ti niečo "
        "nesedí.",
        "Môžeš mi poslať tie čísla pred obedom? Chcem ich dnes poobede prejsť s tímom, ešte pred hovorom s "
        "našimi partnermi. Ak niečo chýba, jednoducho mi zavolaj a pozrieme sa na to spolu. Nemalo by to trvať "
        "dlhšie ako desať minút.",
        "Vďaka, to mi naozaj pomohlo. Myslím, že plán je teraz jasný a všetci vedia, čo ďalej. Ozvime sa znova "
        "budúci týždeň a uvidíme, ako to dopadlo. Pekný večer a pozdravuj odo mňa rodinu.",
    ],
    "sw": [
        "Mkutano umehamishiwa Alhamisi, kwa hiyo tuna siku mbili zaidi za kumaliza slaidi. Nitaandika sehemu ya "
        "kwanza usiku huu na kukutumia asubuhi. Tafadhali ongeza maelezo yako ukipata nafasi, na uniambie kama "
        "kuna kitu kinachoonekana si sawa.",
        "Unaweza kunitumia takwimu kabla ya chakula cha mchana? Nataka kuzikagua pamoja na timu mchana huu, "
        "kabla ya simu na washirika wetu. Kama kuna kitu kinakosekana, nipigie simu tu, na tutaangalia pamoja. "
        "Haipaswi kuchukua zaidi ya dakika kumi.",
        "Asante, hilo limesaidia sana. Nadhani mpango sasa uko wazi, na kila mtu anajua nini cha kufanya "
        "baadaye. Tuzungumze tena wiki ijayo tuone ilivyokwenda. Uwe na jioni njema, na unisalimie familia yako.",
    ],
    "sv": [
        "Mötet har flyttats till torsdag, så vi har två dagar till på oss att göra klart bilderna. Jag skriver "
        "den första delen i kväll och skickar den till dig i morgon bitti. Lägg till dina anteckningar när du "
        "hinner, och säg till om något ser fel ut.",
        "Kan du skicka siffrorna till mig före lunch? Jag vill gå igenom dem med teamet i eftermiddag, innan "
        "samtalet med våra partner. Om något saknas är det bara att ringa mig, så tittar vi på det tillsammans. "
        "Det borde inte ta mer än tio minuter.",
        "Tack, det var verkligen till stor hjälp. Jag tror att planen är tydlig nu och att alla vet vad de ska "
        "göra härnäst. Vi hörs igen nästa vecka och ser hur det gick. Ha en trevlig kväll, och hälsa familjen "
        "från mig.",
    ],
    "tl": [
        "Inilipat sa Huwebes ang pulong, kaya may dalawang araw pa tayo para tapusin ang mga slide. Isusulat ko "
        "ang unang bahagi mamayang gabi at ipapadala ko sa iyo bukas ng umaga. Pakidagdag ang mga tala mo kapag "
        "may oras ka, at sabihin mo sa akin kung may mukhang mali.",
        "Puwede mo bang ipadala sa akin ang mga numero bago mananghalian? Gusto kong suriin ang mga ito kasama "
        "ang team mamayang hapon, bago ang tawag sa ating mga partner. Kung may kulang, tawagan mo lang ako, at "
        "sabay nating titingnan. Hindi ito dapat tumagal nang higit sa sampung minuto.",
        "Salamat, talagang nakatulong iyon. Sa tingin ko, malinaw na ang plano ngayon, at alam na ng lahat ang "
        "susunod na gagawin. Mag-usap ulit tayo sa susunod na linggo at tingnan natin kung paano ito nangyari. "
        "Magandang gabi, at ikumusta mo ako sa pamilya mo.",
    ],
    "ta": [
        "கூட்டம் வியாழக்கிழமைக்கு மாற்றப்பட்டுள்ளது, அதனால் ஸ்லைடுகளை முடிக்க நமக்கு இன்னும் இரண்டு நாட்கள் "
        "உள்ளன. இன்று இரவு முதல் பகுதியை எழுதி, நாளை காலை உனக்கு அனுப்புகிறேன். நேரம் கிடைக்கும்போது உன் "
        "குறிப்புகளைச் சேர், ஏதாவது தவறாகத் தெரிந்தால் எனக்குச் சொல்.",
        "மதிய உணவுக்கு முன் எண்களை எனக்கு அனுப்ப முடியுமா? இன்று மதியம், நம் கூட்டாளிகளுடனான அழைப்புக்கு முன், "
        "அவற்றைக் குழுவுடன் சரிபார்க்க விரும்புகிறேன். ஏதாவது விடுபட்டிருந்தால், எனக்கு ஒரு அழைப்பு கொடு, நாம் "
        "சேர்ந்து பார்க்கலாம். இது பத்து நிமிடங்களுக்கு மேல் ஆகக்கூடாது.",
        "நன்றி, இது உண்மையிலேயே மிகவும் உதவியாக இருந்தது. இப்போது திட்டம் தெளிவாக இருக்கிறது, அடுத்து என்ன செய்ய "
        "வேண்டும் என்று எல்லோருக்கும் தெரியும் என்று நினைக்கிறேன். அடுத்த வாரம் மீண்டும் பேசி, எப்படிப் போனது "
        "என்று பார்ப்போம். இனிய மாலை, உன் குடும்பத்தினருக்கு என் வணக்கத்தைச் சொல்.",
    ],
    "th": [
        "การประชุมเลื่อนไปเป็นวันพฤหัสบดี เราจึงมีเวลาเพิ่มอีกสองวันในการทำสไลด์ให้เสร็จ "
        "คืนนี้จะเขียนส่วนแรกแล้วส่งให้คุณพรุ่งนี้เช้า ถ้ามีเวลาช่วยเพิ่มโน้ตของคุณด้วย "
        "และบอกได้เลยถ้ามีอะไรดูไม่ถูกต้อง",
        "ส่งตัวเลขมาให้ก่อนพักเที่ยงได้ไหม อยากตรวจดูกับทีมบ่ายนี้ ก่อนคุยโทรศัพท์กับพาร์ตเนอร์ของเรา "
        "ถ้ามีอะไรขาดไป โทรหาได้เลย แล้วเราจะดูด้วยกัน ไม่น่าจะใช้เวลาเกินสิบนาที",
        "ขอบคุณมาก ช่วยได้เยอะจริง ๆ ตอนนี้แผนชัดเจนแล้ว และทุกคนก็รู้ว่าต้องทำอะไรต่อ "
        "สัปดาห์หน้ามาคุยกันอีกครั้งแล้วดูว่าเป็นอย่างไรบ้าง ขอให้มีค่ำคืนที่ดี "
        "และฝากความคิดถึงถึงครอบครัวของคุณด้วย",
    ],
    "tr": [
        "Toplantı perşembeye alındı, bu yüzden slaytları bitirmek için iki günümüz daha var. İlk bölümü bu akşam "
        "yazıp sabah sana göndereceğim. Vaktin olunca notlarını ekle ve bir şey yanlış görünürse bana söyle.",
        "Rakamları öğle yemeğinden önce bana gönderebilir misin? Bu öğleden sonra, ortaklarımızla görüşmeden "
        "önce onları ekiple kontrol etmek istiyorum. Eksik bir şey varsa beni araman yeterli, birlikte bakarız. "
        "On dakikadan fazla sürmemesi gerekir.",
        "Teşekkürler, bu gerçekten çok işe yaradı. Bence plan artık net ve herkes bundan sonra ne yapacağını "
        "biliyor. Haftaya tekrar konuşalım ve nasıl geçtiğine bakalım. İyi akşamlar, ailene de benden selam "
        "söyle.",
    ],
    "uk": [
        "Зустріч перенесли на четвер, тож у нас є ще два дні, щоб закінчити слайди. Сьогодні ввечері я напишу "
        "першу частину і вранці надішлю її тобі. Додай свої нотатки, коли зможеш, і скажи мені, якщо щось "
        "виглядає не так.",
        "Можеш надіслати мені цифри до обіду? Я хочу перевірити їх з командою сьогодні вдень, ще до дзвінка з "
        "нашими партнерами. Якщо чогось бракує, просто подзвони мені, і ми подивимося разом. Це має зайняти не "
        "більше ніж десять хвилин.",
        "Дякую, це справді дуже допомогло. Думаю, тепер план зрозумілий, і всі знають, що робити далі. "
        "Поговорімо знову наступного тижня і подивимося, як усе пройшло. Гарного вечора, і передай від мене "
        "вітання своїй родині.",
    ],
    "ur": [
        "میٹنگ جمعرات پر چلی گئی ہے، اس لیے سلائیڈز مکمل کرنے کے لیے ہمارے پاس دو دن اور ہیں۔ میرا ارادہ ہے کہ "
        "آج رات پہلا حصہ لکھوں اور صبح آپ کو بھیج دوں۔ جب وقت ملے اپنے نوٹس شامل کر دیجیے، اور اگر کچھ غلط لگے "
        "تو مجھے بتا دیجیے۔",
        "کیا آپ دوپہر کے کھانے سے پہلے مجھے اعداد و شمار بھیج سکتے ہیں؟ آج سہ پہر، پارٹنرز کے ساتھ کال سے پہلے، "
        "مجھے انہیں ٹیم کے ساتھ دیکھنا ہے۔ اگر کچھ کمی ہو تو بس مجھے فون کر دیجیے، ہم مل کر دیکھ لیں گے۔ اس میں "
        "دس منٹ سے زیادہ نہیں لگنے چاہئیں۔",
        "شکریہ، اس سے واقعی بہت مدد ملی۔ مجھے لگتا ہے کہ اب منصوبہ واضح ہے، اور سب کو معلوم ہے کہ آگے کیا کرنا "
        "ہے۔ آئیے اگلے ہفتے پھر بات کریں اور دیکھیں کہ کیسا رہا۔ آپ کی شام اچھی گزرے، اور اپنے گھر والوں کو میرا "
        "سلام کہیے۔",
    ],
    "vi": [
        "Cuộc họp đã dời sang thứ Năm, nên chúng ta có thêm hai ngày để làm xong các slide. Tối nay tôi sẽ viết "
        "phần đầu tiên và gửi cho bạn vào sáng mai. Khi nào rảnh, bạn thêm ghi chú của mình vào nhé, và nói với "
        "tôi nếu có gì trông không ổn.",
        "Bạn có thể gửi cho tôi các con số trước bữa trưa không? Tôi muốn kiểm tra chúng với nhóm vào chiều nay, "
        "trước cuộc gọi với các đối tác của chúng ta. Nếu thiếu gì, cứ gọi cho tôi, rồi mình cùng xem. Việc này "
        "không nên mất quá mười phút.",
        "Cảm ơn, điều đó thật sự rất hữu ích. Tôi nghĩ bây giờ kế hoạch đã rõ ràng, và mọi người đều biết tiếp "
        "theo phải làm gì. Tuần sau mình nói chuyện lại và xem mọi việc thế nào nhé. Chúc bạn buổi tối vui vẻ, "
        "và cho tôi gửi lời hỏi thăm đến gia đình bạn.",
    ],
}

_CONSENT_WORDS = ("blue", "river", "seven", "green", "window", "three", "orange", "garden", "nine",
                  "silver", "mountain", "five", "yellow", "bridge", "four", "purple", "forest", "eight")


def read_texts(language):
    return READ_TEXTS.get((language or "en")[:2].lower(), READ_TEXTS["en"])


def consent_phrase(rng=None):
    """Three random words the person says at the end of "Record now" (shows it's live and theirs)."""
    rng = rng or random.SystemRandom()
    return " ".join(rng.sample(_CONSENT_WORDS, 3))
