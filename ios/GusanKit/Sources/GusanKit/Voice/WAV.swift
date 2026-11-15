import Foundation

public enum WAV {
    public static func encode(samples: [Int16], sampleRate: Int = 16000) -> Data {
        var data = Data()
        func string(_ value: String) { data.append(contentsOf: value.utf8) }
        func number<T: FixedWidthInteger>(_ value: T) { var le = value.littleEndian; withUnsafeBytes(of: &le) { data.append(contentsOf: $0) } }
        string("RIFF"); number(UInt32(36 + samples.count * 2)); string("WAVEfmt ")
        number(UInt32(16)); number(UInt16(1)); number(UInt16(1)); number(UInt32(sampleRate))
        number(UInt32(sampleRate * 2)); number(UInt16(2)); number(UInt16(16))
        string("data"); number(UInt32(samples.count * 2))
        for sample in samples { number(sample) }
        return data
    }
}
