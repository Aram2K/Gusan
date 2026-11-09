// gusan-tap: the call's sound on macOS for Gusan's Interpreter (Hear direction).
//
// A Core Audio process tap (macOS 14.2+) of everything the Mac plays, except the processes given
// with --exclude-pid (Gusan itself: its own translated voice never comes back). Writes 16 kHz mono
// 16-bit little-endian PCM to stdout, in step with the output device's clock (silence included).
// Needs the "System Audio Recording" permission. Exit codes: 2 = macOS too old, 3 = no permission
// or the tap was refused, 4 = no output device, 5 = the device could not start.
//
//   gusan-tap [--exclude-pid PID]... [--status]
//   --status: print the permission's state (granted / denied / unknown) and exit.
import AudioToolbox
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IONBF, 0)

func say(_ msg: String) {
    FileHandle.standardError.write(("gusan-tap: " + msg + "\n").data(using: .utf8)!)
}

func fail(_ code: Int32, _ msg: String) -> Never {
    say(msg)
    exit(code)
}

// The permission's state, read the way macOS's own TCC framework does (no prompt).
func permissionStatus() -> String {
    guard let h = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW),
          let sym = dlsym(h, "TCCAccessPreflight") else { return "unknown" }
    typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int
    let preflight = unsafeBitCast(sym, to: Preflight.self)
    switch preflight("kTCCServiceAudioCapture" as CFString, nil) {
    case 0: return "granted"
    case 1: return "denied"
    default: return "unknown"
    }
}

var excluded: [pid_t] = []
var args = CommandLine.arguments.dropFirst()
while let a = args.popFirst() {
    switch a {
    case "--exclude-pid":
        if let v = args.popFirst(), let p = pid_t(v) { excluded.append(p) }
    case "--status":
        print(permissionStatus())
        exit(0)
    default:
        fail(64, "unknown argument " + a)
    }
}

guard #available(macOS 14.2, *) else { fail(2, "needs macOS 14.2 or later") }

func addr(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func processObject(_ pid: pid_t) -> AudioObjectID? {
    var a = addr(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var p = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a,
                                        UInt32(MemoryLayout<pid_t>.size), &p, &size, &obj)
    return st == noErr && obj != kAudioObjectUnknown ? obj : nil
}

func defaultOutputUID() -> String? {
    var a = addr(kAudioHardwarePropertyDefaultSystemOutputDevice)
    var dev = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &dev) == noErr,
          dev != kAudioObjectUnknown else { return nil }
    var u = addr(kAudioDevicePropertyDeviceUID)
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &u, 0, nil, &size, &uid) == noErr, let s = uid else { return nil }
    return s.takeRetainedValue() as String
}

let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded.compactMap(processObject))
desc.uuid = UUID()
desc.name = "Gusan call sound"
desc.isPrivate = true
desc.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
var st = AudioHardwareCreateProcessTap(desc, &tapID)
guard st == noErr else { fail(3, "the tap was refused (\(st))") }

var fa = addr(kAudioTapPropertyFormat)
var fmt = AudioStreamBasicDescription()
var fsize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
st = AudioObjectGetPropertyData(tapID, &fa, 0, nil, &fsize, &fmt)
guard st == noErr, fmt.mSampleRate > 0 else { fail(3, "the tap has no format (\(st))") }
guard let outUID = defaultOutputUID() else { fail(4, "no output device") }

let aggUID = UUID().uuidString
let agg: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Gusan Tap",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceMainSubDeviceKey: outUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                       kAudioSubTapUIDKey: desc.uuid.uuidString]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
st = AudioHardwareCreateAggregateDevice(agg as CFDictionary, &aggID)
guard st == noErr else { AudioHardwareDestroyProcessTap(tapID); fail(5, "no aggregate device (\(st))") }

func cleanup() {
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
}

// Mono, then 16 kHz by linear interpolation; the read position carries over between buffers so a
// chunk is never stretched.
let channels = Int(max(1, fmt.mChannelsPerFrame))
let isFloat = fmt.mFormatFlags & kAudioFormatFlagIsFloat != 0
let interleaved = fmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0
let step = fmt.mSampleRate / 16000.0
var pos = 0.0               // next output sample's position in the input stream (input samples)
var consumed = 0.0          // input samples before the current buffer
var last: Float = 0
let writer = DispatchQueue(label: "gusan-tap.write")
let ioQueue = DispatchQueue(label: "gusan-tap.io", qos: .userInteractive)

var procID: AudioDeviceIOProcID?
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) { _, inData, _, _, _ in
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    guard isFloat, abl.count > 0, let p0 = abl[0].mData else { return }
    let frames: Int
    var mono: [Float]
    if interleaved {
        let ch = Int(max(1, abl[0].mNumberChannels))
        frames = Int(abl[0].mDataByteSize) / (4 * ch)
        let src = p0.assumingMemoryBound(to: Float.self)
        mono = [Float](repeating: 0, count: frames)
        for i in 0..<frames {
            var s: Float = 0
            for c in 0..<ch { s += src[i * ch + c] }
            mono[i] = s / Float(ch)
        }
    } else {
        frames = Int(abl[0].mDataByteSize) / 4
        mono = [Float](repeating: 0, count: frames)
        for b in abl {
            guard let d = b.mData else { continue }
            let src = d.assumingMemoryBound(to: Float.self)
            for i in 0..<frames { mono[i] += src[i] }
        }
        let n = Float(abl.count)
        for i in 0..<frames { mono[i] /= n }
    }
    var out = [Int16]()
    out.reserveCapacity(Int(Double(frames) / step) + 2)
    let end = consumed + Double(frames)
    while pos < end - 1 || (pos < end && frames > 0) {
        let x = pos - consumed
        let i = Int(x.rounded(.down))
        let f = Float(x - Double(i))
        let a = i >= 0 ? mono[i] : last
        let b = i + 1 < frames ? mono[i + 1] : mono[frames - 1]
        let v = max(-1, min(1, a + (b - a) * f))
        out.append(Int16(v * 32767))
        pos += step
    }
    if frames > 0 { last = mono[frames - 1] }
    consumed = end
    let data = out.withUnsafeBufferPointer { Data(buffer: $0) }
    writer.async {
        if fwrite((data as NSData).bytes, 1, data.count, stdout) < data.count { cleanup(); exit(0) }
    }
}
guard st == noErr, let proc = procID else { cleanup(); fail(5, "no IO proc (\(st))") }
st = AudioDeviceStart(aggID, proc)
guard st == noErr else { cleanup(); fail(5, "could not start (\(st))") }
say("started: \(fmt.mSampleRate) Hz, \(channels) ch, excluding \(excluded)")

for sig in [SIGTERM, SIGINT, SIGPIPE] {
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler { AudioDeviceStop(aggID, proc); cleanup(); exit(0) }
    s.resume()
    _ = Unmanaged.passRetained(s)
}
// stdin closed = Gusan went away: stop too
DispatchQueue.global().async {
    while FileHandle.standardInput.availableData.count > 0 {}
    AudioDeviceStop(aggID, proc)
    cleanup()
    exit(0)
}
dispatchMain()
