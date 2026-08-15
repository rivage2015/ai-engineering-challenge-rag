#!/usr/bin/env swift

import AppKit
import Foundation
import ImageIO
import Vision

private let runnerVersion = "0.1"
private let requestRevision = VNRecognizeTextRequestRevision3

private struct OCRLine: Codable {
    let sequence: Int
    let raw_text: String
    let bbox: [Int]
    let confidence: Double
}

private struct OCRResult: Codable {
    let status: String
    let runner: String
    let runner_version: String
    let request_revision: Int
    let width_px: Int?
    let height_px: Int?
    let source_orientation: Int?
    let lines: [OCRLine]
    let warnings: [String]
    let error: String?
}

private struct DecodedImage {
    let image: CGImage
    let orientation: CGImagePropertyOrientation
}

private struct EngineInfo: Codable {
    let engine: String
    let runner: String
    let runner_version: String
    let request_revision: Int
    let current_revision: Int
    let recognition_level: String
    let recognition_languages: [String]
    let uses_language_correction: Bool
    let automatically_detects_language: Bool
    let operating_system: String
}

private func emit<T: Encodable>(_ value: T) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(value)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
}

private func quantizedBox(_ box: CGRect) -> [Int] {
    let left = max(0.0, min(1.0, box.origin.x))
    let top = max(0.0, min(1.0, 1.0 - box.origin.y - box.size.height))
    let right = max(left, min(1.0, box.origin.x + box.size.width))
    let bottom = max(top, min(1.0, 1.0 - box.origin.y))

    let x = max(0, min(999, Int(floor(left * 1000.0))))
    let y = max(0, min(999, Int(floor(top * 1000.0))))
    let rightEdge = max(x + 1, min(1000, Int(ceil(right * 1000.0))))
    let bottomEdge = max(y + 1, min(1000, Int(ceil(bottom * 1000.0))))
    return [x, y, rightEdge - x, bottomEdge - y]
}

private func decodeImage(_ data: Data) throws -> DecodedImage {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
        throw NSError(
            domain: "apple-vision-swift-ocr",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "input bytes are not a supported image"]
        )
    }
    guard CGImageSourceGetCount(source) == 1 else {
        throw NSError(
            domain: "apple-vision-swift-ocr",
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "input must contain exactly one image frame"]
        )
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(
            domain: "apple-vision-swift-ocr",
            code: 3,
            userInfo: [NSLocalizedDescriptionKey: "image could not be fully decoded"]
        )
    }
    let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
    let rawOrientation = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.uint32Value ?? 1
    let orientation = CGImagePropertyOrientation(rawValue: rawOrientation) ?? .up
    return DecodedImage(image: image, orientation: orientation)
}

private func recognize(_ data: Data) throws -> OCRResult {
    let decoded = try decodeImage(data)
    let image = decoded.image
    let request = VNRecognizeTextRequest()
    request.revision = requestRevision
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ja-JP", "en-US"]
    request.usesLanguageCorrection = true
    request.automaticallyDetectsLanguage = false

    let handler = VNImageRequestHandler(
        cgImage: image,
        orientation: decoded.orientation,
        options: [:]
    )
    try handler.perform([request])

    var lines: [OCRLine] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        // Preserve the engine's observation byte-for-byte.  Trimming is used
        // only to decide whether the candidate is empty; raw_text itself is
        // not corrected or normalized here.
        let text = candidate.string
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
        lines.append(
            OCRLine(
                sequence: lines.count + 1,
                raw_text: text,
                bbox: quantizedBox(observation.boundingBox),
                confidence: max(0.0, min(1.0, Double(candidate.confidence)))
            )
        )
    }

    var warnings = lines.isEmpty ? ["Apple Vision returned no text lines"] : []
    if decoded.orientation != .up {
        warnings.append(
            "Apple Vision applied source image orientation \(decoded.orientation.rawValue)"
        )
    }
    return OCRResult(
        status: warnings.isEmpty ? "completed" : "needs_review",
        runner: "apple-vision-swift-ocr",
        runner_version: runnerVersion,
        request_revision: Int(requestRevision),
        width_px: image.width,
        height_px: image.height,
        source_orientation: Int(decoded.orientation.rawValue),
        lines: lines,
        warnings: warnings,
        error: nil
    )
}

private func engineInfo() -> EngineInfo {
    let os = ProcessInfo.processInfo.operatingSystemVersionString
    return EngineInfo(
        engine: "apple_vision",
        runner: "apple-vision-swift-ocr",
        runner_version: runnerVersion,
        request_revision: Int(requestRevision),
        current_revision: Int(VNRecognizeTextRequest.currentRevision),
        recognition_level: "accurate",
        recognition_languages: ["ja-JP", "en-US"],
        uses_language_correction: true,
        automatically_detects_language: false,
        operating_system: os
    )
}

do {
    if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--engine-info" {
        try emit(engineInfo())
        exit(EXIT_SUCCESS)
    }
    guard CommandLine.arguments.count == 1 else {
        FileHandle.standardError.write(
            Data("usage: apple_vision_ocr [--engine-info]\nimage bytes are read from stdin\n".utf8)
        )
        exit(2)
    }
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard !input.isEmpty else {
        throw NSError(
            domain: "apple-vision-swift-ocr",
            code: 4,
            userInfo: [NSLocalizedDescriptionKey: "stdin contained no image bytes"]
        )
    }
    try emit(recognize(input))
} catch {
    let failed = OCRResult(
        status: "failed",
        runner: "apple-vision-swift-ocr",
        runner_version: runnerVersion,
        request_revision: Int(requestRevision),
        width_px: nil,
        height_px: nil,
        source_orientation: nil,
        lines: [],
        warnings: ["Apple Vision OCR failed"],
        error: "\(type(of: error)): \(error.localizedDescription)"
    )
    try? emit(failed)
    exit(EXIT_FAILURE)
}
