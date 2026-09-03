#!/usr/bin/env swift

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

private let runner = "aiec-image-canonicalizer"
private let runnerVersion = "0.1"

private struct CanonicalizationResult: Codable {
    let status: String
    let runner: String
    let runner_version: String
    let source_width_px: Int?
    let source_height_px: Int?
    let source_orientation: Int?
    let canonical_width_px: Int?
    let canonical_height_px: Int?
    let canonical_orientation: Int?
    let output_format: String?
    let color_space: String?
    let pixel_format: String?
    let alpha_policy: String?
    let error: String?
}

private func emit(_ value: CanonicalizationResult) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    FileHandle.standardOutput.write(try encoder.encode(value))
    FileHandle.standardOutput.write(Data([0x0a]))
}

private func fail(_ error: Error) -> Never {
    let result = CanonicalizationResult(
        status: "failed",
        runner: runner,
        runner_version: runnerVersion,
        source_width_px: nil,
        source_height_px: nil,
        source_orientation: nil,
        canonical_width_px: nil,
        canonical_height_px: nil,
        canonical_orientation: nil,
        output_format: nil,
        color_space: nil,
        pixel_format: nil,
        alpha_policy: nil,
        error: "\(type(of: error)): \(error.localizedDescription)"
    )
    try? emit(result)
    exit(EXIT_FAILURE)
}

private func canonicalize(_ data: Data, outputURL: URL) throws -> CanonicalizationResult {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
        throw NSError(
            domain: runner,
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "input bytes are not a supported image"]
        )
    }
    guard CGImageSourceGetCount(source) == 1 else {
        throw NSError(
            domain: runner,
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "input must contain exactly one image frame"]
        )
    }
    guard let sourceImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(
            domain: runner,
            code: 3,
            userInfo: [NSLocalizedDescriptionKey: "image could not be fully decoded"]
        )
    }

    let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
        as? [CFString: Any]
    let rawOrientation =
        (properties?[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
    guard (1...8).contains(rawOrientation) else {
        throw NSError(
            domain: runner,
            code: 4,
            userInfo: [NSLocalizedDescriptionKey: "source EXIF orientation is invalid"]
        )
    }

    let thumbnailOptions: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: max(sourceImage.width, sourceImage.height),
        kCGImageSourceShouldCacheImmediately: true,
    ]
    guard let orientedImage = CGImageSourceCreateThumbnailAtIndex(
        source,
        0,
        thumbnailOptions as CFDictionary
    ) else {
        throw NSError(
            domain: runner,
            code: 5,
            userInfo: [NSLocalizedDescriptionKey: "orientation could not be applied"]
        )
    }

    guard let sRGB = CGColorSpace(name: CGColorSpace.sRGB) else {
        throw NSError(
            domain: runner,
            code: 6,
            userInfo: [NSLocalizedDescriptionKey: "sRGB color space is unavailable"]
        )
    }
    guard let context = CGContext(
        data: nil,
        width: orientedImage.width,
        height: orientedImage.height,
        bitsPerComponent: 8,
        bytesPerRow: orientedImage.width * 4,
        space: sRGB,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            | CGBitmapInfo.byteOrder32Big.rawValue
    ) else {
        throw NSError(
            domain: runner,
            code: 7,
            userInfo: [NSLocalizedDescriptionKey: "canonical RGBA8 context could not be created"]
        )
    }
    let canonicalBounds = CGRect(
        x: 0,
        y: 0,
        width: orientedImage.width,
        height: orientedImage.height
    )
    context.setFillColor(CGColor(gray: 1.0, alpha: 1.0))
    context.fill(canonicalBounds)
    context.draw(orientedImage, in: canonicalBounds)
    guard let canonicalImage = context.makeImage() else {
        throw NSError(
            domain: runner,
            code: 12,
            userInfo: [NSLocalizedDescriptionKey: "canonical RGBA8 image could not be created"]
        )
    }

    guard let destination = CGImageDestinationCreateWithURL(
        outputURL as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        throw NSError(
            domain: runner,
            code: 8,
            userInfo: [NSLocalizedDescriptionKey: "PNG output could not be created"]
        )
    }
    let outputProperties: [CFString: Any] = [
        kCGImagePropertyOrientation: 1,
        kCGImagePropertyColorModel: kCGImagePropertyColorModelRGB,
    ]
    CGImageDestinationAddImage(
        destination,
        canonicalImage,
        outputProperties as CFDictionary
    )
    guard CGImageDestinationFinalize(destination) else {
        throw NSError(
            domain: runner,
            code: 9,
            userInfo: [NSLocalizedDescriptionKey: "PNG output could not be finalized"]
        )
    }

    return CanonicalizationResult(
        status: "completed",
        runner: runner,
        runner_version: runnerVersion,
        source_width_px: sourceImage.width,
        source_height_px: sourceImage.height,
        source_orientation: rawOrientation,
        canonical_width_px: canonicalImage.width,
        canonical_height_px: canonicalImage.height,
        canonical_orientation: 1,
        output_format: "PNG",
        color_space: "sRGB",
        pixel_format: "RGBA8",
        alpha_policy: "flattened_on_white",
        error: nil
    )
}

do {
    guard
        CommandLine.arguments.count == 3,
        CommandLine.arguments[1] == "--output"
    else {
        FileHandle.standardError.write(
            Data("usage: image_canonicalizer --output /absolute/path.png\nimage bytes are read from stdin\n".utf8)
        )
        exit(2)
    }
    let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
    guard outputURL.path.hasPrefix("/") else {
        throw NSError(
            domain: runner,
            code: 10,
            userInfo: [NSLocalizedDescriptionKey: "output path must be absolute"]
        )
    }
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard !input.isEmpty else {
        throw NSError(
            domain: runner,
            code: 11,
            userInfo: [NSLocalizedDescriptionKey: "stdin contained no image bytes"]
        )
    }
    try emit(canonicalize(input, outputURL: outputURL))
} catch {
    fail(error)
}
