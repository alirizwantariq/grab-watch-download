// Renders the MediaGrab app icon (1024x1024 PNG) with CoreGraphics.
// macOS convention: rounded-rect artwork (~80% of canvas) on transparent
// margins; the launcher squircle plus a white download-into-tray glyph.
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let size = 1024
let ctx = CGContext(data: nil, width: size, height: size,
                    bitsPerComponent: 8, bytesPerRow: 0,
                    space: CGColorSpace(name: CGColorSpace.sRGB)!,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!

let s = CGFloat(size)

// full-canvas transparent background
ctx.clear(CGRect(x: 0, y: 0, width: s, height: s))

// ── rounded-rect squircle (macOS icon grid: ~824/1024 content) ──────────
let inset: CGFloat = (s - s * 0.8047) / 2        // 100px margins
let radius: CGFloat = s * 0.1807                 // ~185pt corner radius
let body = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
let squircle = CGPath(roundedRect: body, cornerWidth: radius, cornerHeight: radius,
                      transform: nil)
ctx.addPath(squircle)
ctx.clip()

// gradient: sky blue -> deep indigo
let colors = [CGColor(srgbRed: 0.36, green: 0.62, blue: 1.00, alpha: 1),
              CGColor(srgbRed: 0.13, green: 0.29, blue: 0.95, alpha: 1)] as CFArray
let grad = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                      colors: colors, locations: [0, 1])!
ctx.drawLinearGradient(grad, start: CGPoint(x: body.minX, y: body.maxY),
                       end: CGPoint(x: body.maxX, y: body.minY), options: [])

// subtle diagonal sheen (top-left highlight)
let sheen = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                       colors: [CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.22),
                                 CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.0)] as CFArray,
                       locations: [0, 1])!
ctx.saveGState()
ctx.addEllipse(in: CGRect(x: body.minX - body.width * 0.25,
                          y: body.midY,
                          width: body.width * 1.2, height: body.height * 1.2))
ctx.clip()
ctx.drawLinearGradient(sheen, start: CGPoint(x: body.minX, y: body.maxY),
                       end: CGPoint(x: body.midX, y: body.midY), options: [])
ctx.restoreGState()

// ── white download glyph: arrow into a tray ──────────────────────────────
func center(_ p: CGPoint) { ctx.addLine(to: p) }
let white = CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 1)
ctx.setFillColor(white)
ctx.setStrokeColor(white)

let cx = body.midX
// arrow stem (rounded)
let stemTop = body.midY + body.height * 0.16
let stemBottom = body.midY - body.height * 0.10
let stemHalf: CGFloat = body.width * 0.052
let stem = CGPath(roundedRect: CGRect(x: cx - stemHalf, y: stemBottom,
                                      width: stemHalf * 2,
                                      height: stemTop - stemBottom),
                  cornerWidth: stemHalf, cornerHeight: stemHalf, transform: nil)
ctx.addPath(stem)
ctx.fillPath()

// arrow head (chevron)
let headY = body.midY - body.height * 0.04
let headW: CGFloat = body.width * 0.145
let headH: CGFloat = body.height * 0.115
let head = CGMutablePath()
head.move(to: CGPoint(x: cx - headW, y: headY + headH * 0.1))
center(CGPoint(x: cx, y: headY - headH * 0.45))
center(CGPoint(x: cx + headW, y: headY + headH * 0.1))
head.addLine(to: CGPoint(x: cx + headW - body.width * 0.052,
                         y: headY + headH * 0.38))
center(CGPoint(x: cx, y: headY - headH * 0.17))
center(CGPoint(x: cx - headW + body.width * 0.052,
               y: headY + headH * 0.38))
head.closeSubpath()
ctx.addPath(head)
ctx.fillPath()

// tray: rounded underline
let trayY = body.midY - body.height * 0.22
let trayHalf: CGFloat = body.width * 0.17
let trayThick: CGFloat = body.height * 0.032
let tray = CGPath(roundedRect: CGRect(x: cx - trayHalf, y: trayY - trayThick / 2,
                                      width: trayHalf * 2, height: trayThick),
                  cornerWidth: trayThick / 2, cornerHeight: trayThick / 2,
                  transform: nil)
ctx.addPath(tray)
ctx.fillPath()

// speed sparkles left/right of the arrow
func sparkle(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) {
    let p = CGPath(roundedRect: CGRect(x: x, y: y, width: w, height: h),
                   cornerWidth: h / 2, cornerHeight: h / 2, transform: nil)
    ctx.addPath(p)
    ctx.fillPath()
}
ctx.setAlpha(0.55)
sparkle(cx - body.width * 0.235, body.midY + body.height * 0.10,
        body.width * 0.085, body.height * 0.020)
sparkle(cx - body.width * 0.185, body.midY - body.height * 0.01,
        body.width * 0.135, body.height * 0.020)
sparkle(cx + body.width * 0.15, body.midY + body.height * 0.05,
        body.width * 0.085, body.height * 0.020)
sparkle(cx + body.width * 0.10, body.midY - body.height * 0.06,
        body.width * 0.135, body.height * 0.020)
ctx.setAlpha(1.0)

// write PNG
let img = ctx.makeImage()!
let out = URL(fileURLWithPath: CommandLine.arguments.count > 1
              ? CommandLine.arguments[1] : "icon_1024.png")
let dest = CGImageDestinationCreateWithURL(out as CFURL,
                                           UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(dest, img, nil)
CGImageDestinationFinalize(dest)
print("wrote \(out.path)")