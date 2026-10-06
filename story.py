"""
story.py - NINA tells you how a robot is doing, as a bedtime story.

The storyteller is Karpathy's TinyStories model from llama2.c: 15 million
parameters, small enough that a version of it runs on an ESP32. It only knows
how to write simple children's stories, so it can't diagnose anything itself.
NINA writes the opening from what she actually found ("NORA fell asleep and
would not wake up..."), and the little model tells the rest. Text only.

This is the whole model in NumPy: no PyTorch, no GPU, a fraction of a second
per sentence on any PC.

    python story.py NORA "fell asleep and would not wake up"     try it
    tell_story("NORA", ["fell asleep and would not wake up"])   from NINA
"""

import struct
import sys
from pathlib import Path

import numpy as np

MODELS = Path(__file__).resolve().parent / "models"
MODEL_URL = "https://huggingface.co/karpathy/tinyllamas/resolve/main/stories15M.bin"
TOKENIZER_URL = "https://github.com/karpathy/llama2.c/raw/master/tokenizer.bin"


class Tokenizer:
    """llama2.c's tokenizer.bin: a SentencePiece vocabulary with merge scores."""

    def __init__(self, path, vocab_size):
        data = Path(path).read_bytes()
        (self.max_len,) = struct.unpack_from("i", data, 0)
        off = 4
        self.vocab, self.scores = [], []
        for _ in range(vocab_size):
            score, n = struct.unpack_from("fi", data, off)
            off += 8
            self.vocab.append(data[off:off + n].decode("utf-8", errors="replace"))
            self.scores.append(score)
            off += n
        self.index = {t: i for i, t in enumerate(self.vocab)}

    def encode(self, text):
        """BPE the way llama2.c does it: characters first, then keep merging the best-scoring pair."""
        tokens = [self.index[" "]] if " " in self.index else []     # the dummy prefix space
        for ch in text:
            if ch in self.index:
                tokens.append(self.index[ch])
            else:                                                   # unknown: fall back to its bytes
                tokens.extend(b + 3 for b in ch.encode("utf-8"))
        while True:
            best, best_id, best_i = -1e10, -1, -1
            for i in range(len(tokens) - 1):
                merged = self.vocab[tokens[i]] + self.vocab[tokens[i + 1]]
                j = self.index.get(merged)
                if j is not None and self.scores[j] > best:
                    best, best_id, best_i = self.scores[j], j, i
            if best_i < 0:
                return tokens
            tokens[best_i:best_i + 2] = [best_id]

    def decode(self, prev, token):
        piece = self.vocab[token]
        if prev == 1 and piece.startswith(" "):                     # after BOS, drop the leading space
            piece = piece[1:]
        if piece.startswith("<0x") and piece.endswith(">"):         # raw byte tokens
            return bytes([int(piece[3:-1], 16)]).decode("utf-8", errors="ignore")
        return piece


class TinyLlama:
    """llama2.c's model.bin (version 0 format), run with NumPy."""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        dim, hidden, layers, heads, kv_heads, vocab, seq = struct.unpack_from("7i", raw, 0)
        self.shared = vocab > 0
        vocab = abs(vocab)
        self.dim, self.layers, self.heads, self.kv_heads, self.vocab, self.seq = dim, layers, heads, kv_heads, vocab, seq
        self.hs = dim // heads
        kv_dim = dim * kv_heads // heads
        w = np.frombuffer(raw, dtype=np.float32, offset=28)
        pos = 0

        def take(*shape):
            nonlocal pos
            n = int(np.prod(shape))
            a = w[pos:pos + n].reshape(shape)
            pos += n
            return a

        self.emb = take(vocab, dim)
        self.rms_att = take(layers, dim)
        self.wq, self.wk = take(layers, dim, dim), take(layers, kv_dim, dim)
        self.wv, self.wo = take(layers, kv_dim, dim), take(layers, dim, dim)
        self.rms_ffn = take(layers, dim)
        self.w1, self.w2, self.w3 = take(layers, hidden, dim), take(layers, dim, hidden), take(layers, hidden, dim)
        self.rms_final = take(dim)
        take(seq, self.hs // 2)          # the file's precomputed RoPE tables: not needed, computed below
        take(seq, self.hs // 2)
        self.wcls = self.emb if self.shared else take(vocab, dim)
        # RoPE frequencies, as llama2.c's run.c computes them
        idx = np.arange(0, self.hs, 2, dtype=np.float32)
        self.freq = 1.0 / (10000.0 ** (idx / self.hs))
        self.kv_dim = kv_dim

    @staticmethod
    def _rms(x, w):
        return w * (x / np.sqrt(np.mean(x * x) + 1e-5))

    def _rope(self, v, pos, n_heads):
        v = v.reshape(n_heads, self.hs // 2, 2)
        ang = pos * self.freq
        c, s = np.cos(ang), np.sin(ang)
        a, b = v[..., 0], v[..., 1]
        return np.stack([a * c - b * s, a * s + b * c], -1).reshape(-1)

    def generate(self, prompt_tokens, steps=200, temperature=0.8, top_p=0.9, rng=None):
        rng = rng or np.random.default_rng()
        kc = np.zeros((self.layers, self.seq, self.kv_dim), np.float32)
        vc = np.zeros((self.layers, self.seq, self.kv_dim), np.float32)
        tokens = [1] + list(prompt_tokens)           # 1 = BOS
        out, token = [], tokens[0]
        rep = self.heads // self.kv_heads
        for pos in range(min(self.seq, len(tokens) + steps) - 1):
            x = self.emb[token].copy()
            for l in range(self.layers):
                xb = self._rms(x, self.rms_att[l])
                q = self._rope(self.wq[l] @ xb, pos, self.heads)
                kc[l, pos] = self._rope(self.wk[l] @ xb, pos, self.kv_heads)
                vc[l, pos] = self.wv[l] @ xb
                qh = q.reshape(self.heads, self.hs)
                kh = kc[l, :pos + 1].reshape(pos + 1, self.kv_heads, self.hs).repeat(rep, axis=1)
                vh = vc[l, :pos + 1].reshape(pos + 1, self.kv_heads, self.hs).repeat(rep, axis=1)
                att = np.einsum("hd,thd->ht", qh, kh) / np.sqrt(self.hs)
                att = np.exp(att - att.max(axis=1, keepdims=True))
                att /= att.sum(axis=1, keepdims=True)
                x = x + self.wo[l] @ np.einsum("ht,thd->hd", att, vh).reshape(-1)
                xb = self._rms(x, self.rms_ffn[l])
                h1 = self.w1[l] @ xb
                x = x + self.w2[l] @ ((h1 / (1.0 + np.exp(-h1))) * (self.w3[l] @ xb))
            if pos + 1 < len(tokens):                 # still reading the prompt
                token = tokens[pos + 1]
                continue
            logits = self.wcls @ self._rms(x, self.rms_final)
            if temperature <= 0:
                nxt = int(np.argmax(logits))
            else:
                p = np.exp((logits - logits.max()) / temperature)
                p /= p.sum()
                order = np.argsort(-p)
                keep = order[: max(1, int(np.searchsorted(np.cumsum(p[order]), top_p)) + 1)]
                nxt = int(rng.choice(keep, p=p[keep] / p[keep].sum()))
            if nxt == 1:                              # BOS again = the story is over
                break
            out.append(nxt)
            token = nxt
        return out


_model = _tok = None


def ready():
    return (MODELS / "stories15M.bin").exists() and (MODELS / "tokenizer.bin").exists()


def download():
    import urllib.request
    MODELS.mkdir(exist_ok=True)
    for url, name in ((MODEL_URL, "stories15M.bin"), (TOKENIZER_URL, "tokenizer.bin")):
        if not (MODELS / name).exists():
            urllib.request.urlretrieve(url, MODELS / name)


def tell_story(robot, troubles, steps=180, seed=None):
    """A short story about `robot` with `troubles` (plain phrases: "fell asleep and would not wake up")."""
    global _model, _tok
    if _model is None:
        if not ready():
            download()
        _model = TinyLlama(MODELS / "stories15M.bin")
        _tok = Tokenizer(MODELS / "tokenizer.bin", _model.vocab)
    name = robot.capitalize() if robot.isupper() and len(robot) > 3 else robot
    if troubles:
        opening = f"Once upon a time, there was a little robot named {name}. One day, {name} " + f" and ".join(troubles[:2]) + "."
    else:
        opening = f"Once upon a time, there was a little robot named {name}. {name} was happy and worked very well."
    tokens = _tok.encode(opening)
    rng = np.random.default_rng(seed)
    for _ in range(4):          # TinyStories can be grim: a status report shouldn't end with a funeral
        new = _model.generate(tokens, steps=steps, rng=rng)
        text, prev = opening, tokens[-1] if tokens else 1
        for t in new:
            text += _tok.decode(prev, t)
            prev = t
        cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"))      # end on a whole sentence
        text = text[:cut + 1] if cut > len(opening) else text
        if not any(w in text.lower() for w in GRIM):
            break
    return text


GRIM = (" dead", " died", " die ", " dies", " killed", " kill ", " buried", " funeral")


if __name__ == "__main__":
    import time
    who = sys.argv[1] if len(sys.argv) > 1 else "NORA"
    what = sys.argv[2:] or ["fell asleep and would not wake up"]
    t = time.time()
    print(tell_story(who, what, seed=1))
    print(f"\n({time.time() - t:.1f} s)")
