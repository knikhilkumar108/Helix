/**
 * Money + Cost. Micro-unit integer math; no floats. Mirrors the Python impl.
 */

export type Currency = string;

export class CurrencyError extends Error {}
export class AmountError extends Error {}

const MAX = (1n << 63n) - 1n;
const MIN = -(1n << 63n);

export class Money {
  readonly micro: bigint;
  readonly currency: Currency;

  constructor(micro: bigint | number | string, currency: Currency) {
    const m = typeof micro === "bigint" ? micro : BigInt(micro);
    if (m < MIN || m > MAX) throw new AmountError("micro out of range");
    if (!currency) throw new CurrencyError("currency required");
    this.micro = m;
    this.currency = currency.toUpperCase();
  }

  static zero(currency: Currency = "USDC"): Money {
    return new Money(0n, currency);
  }

  static fromMajor(major: number | string, currency: Currency = "USDC"): Money {
    // Convert with 6-decimal precision using bigint math.
    const [whole, frac = ""] = String(major).split(".");
    const fracPadded = (frac + "000000").slice(0, 6);
    const micro = BigInt(whole) * 1_000_000n + BigInt(fracPadded || "0") * BigInt(frac.length < 6 ? 1 : 1);
    return new Money(micro, currency);
  }

  toMajor(): string {
    const sign = this.micro < 0n ? "-" : "";
    const abs = this.micro < 0n ? -this.micro : this.micro;
    const whole = abs / 1_000_000n;
    const frac = (abs % 1_000_000n).toString().padStart(6, "0");
    return `${sign}${whole.toString()}.${frac}`;
  }

  toString(): string {
    return `${this.toMajor()} ${this.currency}`;
  }

  private assertSame(o: Money): void {
    if (this.currency !== o.currency)
      throw new CurrencyError(`currency mismatch: ${this.currency} vs ${o.currency}`);
  }

  add(o: Money): Money {
    this.assertSame(o);
    return new Money(this.micro + o.micro, this.currency);
  }
  sub(o: Money): Money {
    this.assertSame(o);
    return new Money(this.micro - o.micro, this.currency);
  }
  neg(): Money {
    return new Money(-this.micro, this.currency);
  }
  mul(n: number | bigint): Money {
    const factor = typeof n === "bigint" ? n : BigInt(n);
    return new Money(this.micro * factor, this.currency);
  }

  lt(o: Money): boolean {
    this.assertSame(o);
    return this.micro < o.micro;
  }
  le(o: Money): boolean {
    this.assertSame(o);
    return this.micro <= o.micro;
  }
  gt(o: Money): boolean {
    this.assertSame(o);
    return this.micro > o.micro;
  }
  ge(o: Money): boolean {
    this.assertSame(o);
    return this.micro >= o.micro;
  }
  isZero(): boolean {
    return this.micro === 0n;
  }

  equals(o: Money): boolean {
    return this.micro === o.micro && this.currency === o.currency;
  }
}

export type CostKind =
  | "cpu_ms"
  | "gpu_ms"
  | "net_bytes"
  | "disk_bytes"
  | "api_call"
  | "tool_ms";

export class Cost {
  constructor(public ru: bigint, public kind: CostKind) {
    if (ru < 0n) throw new AmountError("ru must be non-negative");
  }
  add(o: Cost): Cost {
    if (o.kind !== this.kind)
      throw new AmountError(`cannot add costs: ${this.kind} + ${o.kind}`);
    return new Cost(this.ru + o.ru, this.kind);
  }
  mul(n: number | bigint): Cost {
    const f = typeof n === "bigint" ? n : BigInt(n);
    return new Cost(this.ru * f, this.kind);
  }
}
