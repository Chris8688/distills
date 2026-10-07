// ocrpdf: render PDF pages (or images) with PDFKit and OCR them with Apple Vision (zh-Hans + en).
// usage: ocrpdf <input.pdf|image> <out.md> [scale=2.0] [firstPage [lastPage] | list:1,5,9]
// Output: markdown with "<!-- page N -->" markers. Pages run concurrently.
import Foundation
import PDFKit
import Vision
import AppKit

func ocrTile(_ cg: CGImage) -> String {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    let h = VNImageRequestHandler(cgImage: cg, options: [:])
    try? h.perform([req])
    guard let obs = req.results else { return "" }
    // group into lines by vertical position, then left-to-right
    let items = obs.compactMap { o -> (CGRect, String)? in
        guard let t = o.topCandidates(1).first?.string else { return nil }
        return (o.boundingBox, t)
    }.sorted { a, b in
        if abs(a.0.midY - b.0.midY) > min(a.0.height, b.0.height) * 0.5 { return a.0.midY > b.0.midY }
        return a.0.minX < b.0.minX
    }
    var lines: [String] = []
    var cur: [String] = []
    var curY: CGFloat? = nil
    var curH: CGFloat = 0
    for (r, t) in items {
        if let y = curY, abs(r.midY - y) <= min(curH, r.height) * 0.5 {
            cur.append(t)
        } else {
            if !cur.isEmpty { lines.append(cur.joined(separator: " ")) }
            cur = [t]; curY = r.midY; curH = r.height
        }
    }
    if !cur.isEmpty { lines.append(cur.joined(separator: " ")) }
    return lines.joined(separator: "\n")
}

// 长截图切片：tile 高 = 宽 × 1.4，重叠 60px；去掉与上一片末行重复的首行
func ocr(_ cg: CGImage) -> String {
    let w = cg.width, h = cg.height
    let tileH = max(Int(Double(w) * 1.4), 1200)
    if h <= Int(Double(tileH) * 1.3) { return ocrTile(cg) }
    var out: [String] = []
    var y = 0
    let overlap = 60
    while y < h {
        let th = min(tileH, h - y)
        if let tile = cg.cropping(to: CGRect(x: 0, y: y, width: w, height: th)) {
            var lines = ocrTile(tile).split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            while let f = lines.first, let l = out.last, !f.isEmpty, (f == l || l.hasSuffix(f) || f.hasPrefix(l)) { lines.removeFirst() }
            out.append(contentsOf: lines)
        }
        if y + th >= h { break }
        y += th - overlap
    }
    return out.joined(separator: "\n")
}

func render(_ page: PDFPage, scale: CGFloat) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    var w = box.width * scale, h = box.height * scale
    // 宽度定在 <= 2400px；超长页不按高度压缩（交给 ocr() 切片），只限总像素 ~120M
    if w > 2400 { let f = 2400 / w; w *= f; h *= f }
    if w * h > 120_000_000 { let f = sqrt(120_000_000 / (w * h)); w *= f; h *= f }
    let cs = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(data: nil, width: Int(w), height: Int(h), bitsPerComponent: 8, bytesPerRow: 0,
                              space: cs, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: w / box.width, y: h / box.height)
    ctx.translateBy(x: -box.minX, y: -box.minY)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: ocrpdf in out [scale] [first] [last]\n".data(using: .utf8)!); exit(2) }
let inURL = URL(fileURLWithPath: args[1])
let scale = CGFloat(Double(args.count > 3 ? args[3] : "2.0") ?? 2.0)
var results: [Int: String] = [:]
let lock = NSLock()
let ext = inURL.pathExtension.lowercased()
if ext == "pdf" {
    guard let doc = PDFDocument(url: inURL) else { FileHandle.standardError.write("cannot open pdf\n".data(using: .utf8)!); exit(1) }
    let n = doc.pageCount
    var idx: [Int] = []
    if args.count > 4 && args[4].hasPrefix("list:") {
        idx = args[4].dropFirst(5).split(separator: ",").compactMap { Int($0) }.filter { $0 >= 1 && $0 <= n }
    } else {
        let first = max(1, Int(args.count > 4 ? args[4] : "1") ?? 1)
        let last = min(n, Int(args.count > 5 ? args[5] : "\(n)") ?? n)
        if first <= last { idx = Array(first...last) }
    }
    if idx.isEmpty { exit(0) }
    DispatchQueue.concurrentPerform(iterations: idx.count) { i in
        let p = idx[i]
        autoreleasepool {
            var text = ""
            if let page = doc.page(at: p - 1), let cg = render(page, scale: scale) { text = ocr(cg) }
            lock.lock(); results[p] = text; lock.unlock()
        }
    }
} else {
    guard let img = NSImage(contentsOf: inURL), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }
    results[1] = ocr(cg)
}
var out = ""
for k in results.keys.sorted() { out += "<!-- page \(k) -->\n" + results[k]! + "\n\n" }
try! out.write(to: URL(fileURLWithPath: args[2]), atomically: true, encoding: .utf8)
