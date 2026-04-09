import math
import os
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pygame


# ============================================================
# Top-Down Roguelike Shooter Prototype (single-file, no assets)
# - WASD move (8-dir), Mouse aim, LMB shoot, Space dodge roll
# - I-frames during roll
# - "Siphon" reload: magazine refills ONLY on Perfect Dodge
# - 5 procedural rooms -> boss room with 2 patterns
# - Procedural art + generated square-wave SFX (no files)
# ============================================================


# -----------------------------
# Global tuning / constants
# -----------------------------
# Default target: 1080p (toggle fullscreen with F11)
WIDTH, HEIGHT = 1920, 1080
FPS = 60

WORLD_MARGIN = 40  # outer wall thickness / arena margin

PLAYER_RADIUS = 14
PLAYER_SPEED = 260.0

ROLL_DURATION = 0.30
ROLL_SPEED = 520.0
ROLL_COOLDOWN = 0.35

# I-frames live only in the middle of the roll, not the full roll.
# Increased i-frames to make rolls more reliable and readable.
IFRAME_START = 0.10
IFRAME_END = 0.50

# Perfect Dodge is tighter than i-frames. Timing is "high-skill"
# but still achievable at 60 FPS.
PERFECT_WINDOW_START = 0.20
PERFECT_WINDOW_END = 0.32

ENEMY_RADIUS = 14
ENEMY_SPEED = 110.0
ENEMY_HP = 3

BOSS_RADIUS = 30
BOSS_HP = 120

PROJECTILE_RADIUS = 5
PROJECTILE_SPEED_PLAYER = 620.0
PROJECTILE_SPEED_ENEMY = 320.0

HITSTOP_ON_HIT = 0.035

ROOMS_BEFORE_BOSS = 3


# -----------------------------
# Small utilities
# -----------------------------
Vec2 = pygame.math.Vector2


def clamp(x: float, a: float, b: float) -> float:
    return a if x < a else b if x > b else x


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease_out_quad(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) * (1.0 - t)


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def angle_to(v: Vec2) -> float:
    return math.atan2(v.y, v.x)


def from_angle(a: float) -> Vec2:
    return Vec2(math.cos(a), math.sin(a))


def circle_vs_circle(pa: Vec2, ra: float, pb: Vec2, rb: float) -> bool:
    return (pa - pb).length_squared() <= (ra + rb) * (ra + rb)


def circle_vs_rect(p: Vec2, r: float, rect: pygame.Rect) -> Tuple[bool, Vec2]:
    # Returns (overlap?, push_out_vector)
    closest_x = clamp(p.x, rect.left, rect.right)
    closest_y = clamp(p.y, rect.top, rect.bottom)
    closest = Vec2(closest_x, closest_y)
    delta = p - closest
    d2 = delta.length_squared()
    if d2 > r * r:
        return False, Vec2()
    if d2 == 0:
        # Center is inside rect; push out toward smallest penetration axis.
        left = abs(p.x - rect.left)
        right = abs(rect.right - p.x)
        top = abs(p.y - rect.top)
        bottom = abs(rect.bottom - p.y)
        m = min(left, right, top, bottom)
        if m == left:
            return True, Vec2(-r, 0)
        if m == right:
            return True, Vec2(r, 0)
        if m == top:
            return True, Vec2(0, -r)
        return True, Vec2(0, r)
    dist = math.sqrt(d2)
    push = delta / dist * (r - dist)
    return True, push


def resolve_circle_walls(pos: Vec2, radius: float, walls: List[pygame.Rect]) -> Vec2:
    # Iterative push-out against rectangles.
    for _ in range(3):
        moved = False
        for w in walls:
            hit, push = circle_vs_rect(pos, radius, w)
            if hit:
                pos += push
                moved = True
        if not moved:
            break
    return pos


def soft_normalize(v: Vec2) -> Vec2:
    if v.length_squared() < 1e-9:
        return Vec2()
    return v.normalize()


def draw_glow_circle(surf: pygame.Surface, pos: Tuple[int, int], base_r: int, color: Tuple[int, int, int], glow: int):
    # Cheap glow: draw multiple translucent circles.
    for i in range(glow, 0, -1):
        a = int(20 * (i / glow))
        c = (*color, a)
        pygame.draw.circle(surf, c, pos, base_r + i)
    pygame.draw.circle(surf, (*color, 220), pos, base_r)


# -----------------------------
# Procedural audio (square waves)
# -----------------------------
class AudioBank:
    def __init__(self):
        self.enabled = False
        self.sounds: Dict[str, Optional[pygame.mixer.Sound]] = {}
        self._try_init()

    def _try_init(self):
        try:
            pygame.mixer.init()
            self.enabled = True
        except Exception:
            self.enabled = False

        self.sounds["shoot"] = self._make_square("shoot", freq=880, ms=55, vol=0.25)
        self.sounds["roll"] = self._make_square("roll", freq=220, ms=80, vol=0.35)
        self.sounds["hit"] = self._make_square("hit", freq=120, ms=90, vol=0.4)
        self.sounds["perfect"] = self._make_square("perfect", freq=1320, ms=80, vol=0.5)
        self.sounds["dry"] = self._make_square("dry", freq=360, ms=35, vol=0.2)

    def _make_square(self, _name: str, freq: float, ms: int, vol: float) -> Optional[pygame.mixer.Sound]:
        if not self.enabled:
            return None
        sr = 44100
        n = int(sr * (ms / 1000.0))
        if n <= 0:
            n = 1
        # 16-bit signed PCM, mono.
        buf = bytearray()
        period = sr / max(1.0, freq)
        amp = int(32767 * clamp(vol, 0.0, 1.0))
        # Quick linear decay envelope to avoid clicks.
        for i in range(n):
            env = 1.0 - (i / n)
            v = amp if (i % int(period)) < (period / 2) else -amp
            v = int(v * env)
            buf += int(v).to_bytes(2, byteorder="little", signed=True)
        try:
            return pygame.mixer.Sound(buffer=bytes(buf))
        except Exception:
            return None

    def play(self, key: str):
        s = self.sounds.get(key)
        if s is not None:
            s.play()


# -----------------------------
# Procedural SpriteSheet + Anim
# -----------------------------
class SpriteSheet:
    """
    Asset-free animation approach:
    - Build a single sheet surface at runtime (grid of frames).
    - Slice frames by rect, then animate by choosing index from time.
    This keeps everything in one file and gives a "real" animation system.
    """

    def __init__(self, frame_w: int, frame_h: int):
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.sheet = pygame.Surface((frame_w * 12, frame_h * 4), pygame.SRCALPHA)
        self.anims: Dict[str, Tuple[List[pygame.Surface], float]] = {}
        self._build()

    def _slice_row(self, row: int, count: int) -> List[pygame.Surface]:
        out = []
        for i in range(count):
            r = pygame.Rect(i * self.frame_w, row * self.frame_h, self.frame_w, self.frame_h)
            out.append(self.sheet.subsurface(r).copy())
        return out

    def _build(self):
        # Rows:
        # 0: player idle (4)
        # 1: player run (6)
        # 2: player roll (6)
        # 3: enemy walk (6) / boss can reuse scaled
        fw, fh = self.frame_w, self.frame_h

        def draw_player_frame(x0: int, y0: int, phase: float, mode: str):
            cx, cy = x0 + fw // 2, y0 + fh // 2
            base = pygame.Rect(x0, y0, fw, fh)
            pygame.draw.rect(self.sheet, (0, 0, 0, 0), base)

            # Body palette
            body = (60, 205, 255)
            body2 = (20, 120, 170)
            outline = (10, 20, 35)
            accent = (255, 240, 130)

            # Subtle bob
            bob = int(math.sin(phase * math.tau) * (2 if mode != "roll" else 1))
            # Legs/feet hint (run)
            step = math.sin(phase * math.tau) if mode == "run" else 0.0
            foot_dx = int(step * 3)

            # Shadow
            pygame.draw.ellipse(self.sheet, (0, 0, 0, 60), (cx - 12, cy + 12, 24, 8))

            # Torso
            pygame.draw.circle(self.sheet, outline, (cx, cy + bob), 14)
            pygame.draw.circle(self.sheet, body2, (cx, cy + bob), 13)
            pygame.draw.circle(self.sheet, body, (cx - 2, cy - 2 + bob), 10)

            # Face / visor
            pygame.draw.ellipse(self.sheet, (0, 0, 0, 160), (cx - 7, cy - 6 + bob, 14, 9))
            pygame.draw.circle(self.sheet, accent, (cx + 4, cy - 2 + bob), 2)

            # Gun (simple)
            gx = cx + 10
            gy = cy + 3 + bob
            pygame.draw.rect(self.sheet, outline, (gx, gy, 12, 4), border_radius=2)
            pygame.draw.rect(self.sheet, (150, 180, 200), (gx + 2, gy + 1, 9, 2), border_radius=2)

            # Feet
            if mode == "run":
                pygame.draw.circle(self.sheet, outline, (cx - 6 + foot_dx, cy + 14 + bob), 4)
                pygame.draw.circle(self.sheet, outline, (cx + 6 - foot_dx, cy + 14 + bob), 4)
                pygame.draw.circle(self.sheet, (230, 230, 230), (cx - 6 + foot_dx, cy + 14 + bob), 3)
                pygame.draw.circle(self.sheet, (230, 230, 230), (cx + 6 - foot_dx, cy + 14 + bob), 3)

        def draw_enemy_frame(x0: int, y0: int, phase: float):
            cx, cy = x0 + fw // 2, y0 + fh // 2
            outline = (20, 10, 10)
            shell = (240, 70, 85)
            shell2 = (150, 25, 40)
            eye = (255, 245, 235)

            bob = int(math.sin(phase * math.tau) * 2)
            pygame.draw.ellipse(self.sheet, (0, 0, 0, 60), (cx - 12, cy + 12, 24, 8))

            pygame.draw.circle(self.sheet, outline, (cx, cy + bob), 14)
            pygame.draw.circle(self.sheet, shell2, (cx, cy + bob), 13)
            pygame.draw.circle(self.sheet, shell, (cx - 2, cy - 2 + bob), 10)

            pygame.draw.circle(self.sheet, eye, (cx - 4, cy - 2 + bob), 2)
            pygame.draw.circle(self.sheet, eye, (cx + 4, cy - 2 + bob), 2)
            pygame.draw.circle(self.sheet, (0, 0, 0), (cx - 4, cy - 2 + bob), 1)
            pygame.draw.circle(self.sheet, (0, 0, 0), (cx + 4, cy - 2 + bob), 1)

            # Little antenna
            pygame.draw.line(self.sheet, outline, (cx, cy - 16 + bob), (cx, cy - 22 + bob), 2)
            pygame.draw.circle(self.sheet, (255, 240, 130), (cx, cy - 23 + bob), 2)

        # Player idle row
        for i in range(4):
            phase = i / 4.0
            draw_player_frame(i * fw, 0 * fh, phase, "idle")

        # Player run row
        for i in range(6):
            phase = i / 6.0
            draw_player_frame(i * fw, 1 * fh, phase, "run")

        # Player roll row (base frames; squash/stretch done at render-time too)
        for i in range(6):
            phase = i / 6.0
            draw_player_frame(i * fw, 2 * fh, phase, "roll")
            # Add a streak hint
            x0, y0 = i * fw, 2 * fh
            pygame.draw.ellipse(self.sheet, (60, 205, 255, 60), (x0 + 10, y0 + fh // 2 + 6, 18, 6))

        # Enemy row
        for i in range(6):
            phase = i / 6.0
            draw_enemy_frame(i * fw, 3 * fh, phase)

        self.anims["player_idle"] = (self._slice_row(0, 4), 6.0)
        self.anims["player_run"] = (self._slice_row(1, 6), 12.0)
        self.anims["player_roll"] = (self._slice_row(2, 6), 16.0)
        self.anims["enemy_walk"] = (self._slice_row(3, 6), 8.0)

    def get_frame(self, anim: str, t: float) -> pygame.Surface:
        frames, fps = self.anims[anim]
        idx = int(t * fps) % len(frames)
        return frames[idx]


# -----------------------------
# VFX: particles + muzzle flash
# -----------------------------
@dataclass
class Particle:
    pos: Vec2
    vel: Vec2
    life: float
    max_life: float
    color: Tuple[int, int, int]
    radius: float

    def update(self, dt: float):
        self.pos += self.vel * dt
        self.vel *= pow(0.1, dt)  # quick damping
        self.life -= dt

    def draw(self, surf: pygame.Surface, cam: Vec2):
        if self.life <= 0:
            return
        t = 1.0 - (self.life / self.max_life)
        a = int(255 * (1.0 - t))
        r = max(1, int(self.radius * (1.0 - 0.6 * t)))
        pygame.draw.circle(surf, (*self.color, a), (int(self.pos.x + cam.x), int(self.pos.y + cam.y)), r)


@dataclass
class MuzzleFlash:
    pos: Vec2
    ang: float
    life: float

    def update(self, dt: float):
        self.life -= dt

    def draw(self, surf: pygame.Surface, cam: Vec2):
        if self.life <= 0:
            return
        t = clamp(self.life / 0.06, 0.0, 1.0)
        size = int(10 + (1 - t) * 10)
        a = int(255 * t)
        d = from_angle(self.ang)
        p0 = Vec2(self.pos)
        p1 = p0 + d * (size * 1.4)
        left = p0 + Vec2(-d.y, d.x) * size * 0.6
        right = p0 + Vec2(d.y, -d.x) * size * 0.6
        pts = [(p1.x + cam.x, p1.y + cam.y), (left.x + cam.x, left.y + cam.y), (right.x + cam.x, right.y + cam.y)]
        pygame.draw.polygon(surf, (255, 245, 160, a), pts)


# -----------------------------
# Projectile
# -----------------------------
class Projectile:
    def __init__(self, owner: str, pos: Vec2, vel: Vec2, damage: int, radius: float = PROJECTILE_RADIUS, life: float = 2.2):
        self.owner = owner  # "player" or "enemy"
        self.pos = Vec2(pos)
        self.vel = Vec2(vel)
        self.damage = damage
        self.radius = radius
        self.life = life
        self.active = True  # used for perfect-dodge checks

    def update(self, dt: float):
        self.pos += self.vel * dt
        self.life -= dt
        if self.life <= 0:
            self.active = False

    def draw(self, surf: pygame.Surface, cam: Vec2):
        c = (90, 220, 255) if self.owner == "player" else (255, 120, 110)
        x, y = int(self.pos.x + cam.x), int(self.pos.y + cam.y)
        pygame.draw.circle(surf, (0, 0, 0, 120), (x + 2, y + 2), int(self.radius + 2))
        draw_glow_circle(surf, (x, y), int(self.radius), c, glow=6)


# -----------------------------
# Weapons
# -----------------------------
@dataclass
class WeaponConfig:
    name: str
    fire_rate: float  # shots per second
    spread_deg: float
    bullet_speed: float
    damage: int
    mag_size: int
    kick: float  # screen shake trauma on shot


WEAPONS = [
    WeaponConfig(name="Pistol", fire_rate=5.0, spread_deg=2.0, bullet_speed=PROJECTILE_SPEED_PLAYER, damage=1, mag_size=10, kick=0.12),
    WeaponConfig(name="SMG", fire_rate=12.0, spread_deg=6.0, bullet_speed=PROJECTILE_SPEED_PLAYER * 0.92, damage=1, mag_size=24, kick=0.09),
]


# -----------------------------
# Player
# -----------------------------
class Player:
    def __init__(self, pos: Vec2, sheet: SpriteSheet, audio: AudioBank):
        self.pos = Vec2(pos)
        self.vel = Vec2()
        self.radius = PLAYER_RADIUS
        self.hp_max = 6
        self.hp = self.hp_max
        self.invuln_timer = 0.0  # after taking damage (not roll iframes)
        self.knock = Vec2()

        self.sheet = sheet
        self.anim_t = 0.0
        self.facing = Vec2(1, 0)

        # Roll state
        self.rolling = False
        self.roll_t = 0.0
        self.roll_dir = Vec2(1, 0)
        self.roll_cooldown = 0.0
        self.perfect_dodge_consumed = False
        self.perfect_flash = 0.0  # UI/VFX pulse

        # Weapon / ammo
        self.weapon_idx = 0
        self.weapon = WEAPONS[self.weapon_idx]
        self.ammo_in_mag = self.weapon.mag_size
        self.shot_cooldown = 0.0

        self.audio = audio

    def switch_weapon(self):
        self.weapon_idx = (self.weapon_idx + 1) % len(WEAPONS)
        self.weapon = WEAPONS[self.weapon_idx]
        # Keep siphon mechanic meaningful: switching doesn't grant ammo.
        self.ammo_in_mag = min(self.ammo_in_mag, self.weapon.mag_size)

    def is_in_iframes(self) -> bool:
        if not self.rolling:
            return False
        return IFRAME_START <= self.roll_t <= IFRAME_END

    def is_in_perfect_window(self) -> bool:
        if not self.rolling:
            return False
        return PERFECT_WINDOW_START <= self.roll_t <= PERFECT_WINDOW_END

    def can_shoot(self) -> bool:
        return (not self.rolling) and self.hp > 0

    def start_roll(self, input_dir: Vec2):
        if self.rolling or self.roll_cooldown > 0:
            return
        d = soft_normalize(input_dir)
        if d.length_squared() < 1e-9:
            d = soft_normalize(self.facing)
            if d.length_squared() < 1e-9:
                d = Vec2(1, 0)
        self.rolling = True
        self.roll_t = 0.0
        self.roll_dir = d
        self.perfect_dodge_consumed = False
        self.audio.play("roll")

    def apply_damage(self, dmg: int, knock_dir: Vec2):
        if self.hp <= 0:
            return
        if self.invuln_timer > 0:
            return
        self.hp = max(0, self.hp - dmg)
        self.invuln_timer = 0.6
        self.knock = soft_normalize(knock_dir) * 220.0
        self.audio.play("hit")

    def try_siphon_reload(self) -> bool:
        # Only triggers once per roll.
        if self.perfect_dodge_consumed:
            return False
        self.perfect_dodge_consumed = True
        self.ammo_in_mag = self.weapon.mag_size
        self.perfect_flash = 0.7
        self.audio.play("perfect")
        return True

    def update(self, dt: float, walls: List[pygame.Rect], mouse_world: Vec2, keys: pygame.key.ScancodeWrapper):
        if self.hp <= 0:
            # Soft fall to stop.
            self.vel *= pow(0.05, dt)
            self.knock *= pow(0.05, dt)
            self.pos += (self.vel + self.knock) * dt
            self.pos = resolve_circle_walls(self.pos, self.radius, walls)
            if self.invuln_timer > 0:
                self.invuln_timer = max(0.0, self.invuln_timer - dt)
            if self.perfect_flash > 0:
                self.perfect_flash = max(0.0, self.perfect_flash - dt)
            return

        self.anim_t += dt
        if self.invuln_timer > 0:
            self.invuln_timer = max(0.0, self.invuln_timer - dt)

        if self.roll_cooldown > 0:
            self.roll_cooldown = max(0.0, self.roll_cooldown - dt)

        if self.perfect_flash > 0:
            self.perfect_flash = max(0.0, self.perfect_flash - dt)

        # Aim/facing always points toward mouse in world space.
        aim = mouse_world - self.pos
        if aim.length_squared() > 1e-6:
            self.facing = aim.normalize()

        # Input movement (8-dir)
        move = Vec2(
            (1 if keys[pygame.K_d] else 0) - (1 if keys[pygame.K_a] else 0),
            (1 if keys[pygame.K_s] else 0) - (1 if keys[pygame.K_w] else 0),
        )
        move = soft_normalize(move)

        if self.rolling:
            self.roll_t += dt
            t = self.roll_t / max(1e-6, ROLL_DURATION)
            # Slight ease to feel punchy at the start.
            speed = ROLL_SPEED * lerp(1.15, 0.85, ease_out_quad(t))
            self.vel = self.roll_dir * speed
            if self.roll_t >= ROLL_DURATION:
                self.rolling = False
                self.roll_cooldown = ROLL_COOLDOWN
        else:
            # Standard movement with acceleration feel.
            target = move * PLAYER_SPEED
            self.vel = self.vel.lerp(target, 1 - pow(0.0005, dt))

        # Knockback decay (hit reaction)
        self.knock *= pow(0.02, dt)

        self.pos += (self.vel + self.knock) * dt
        self.pos = resolve_circle_walls(self.pos, self.radius, walls)

        if self.shot_cooldown > 0:
            self.shot_cooldown = max(0.0, self.shot_cooldown - dt)

    def try_shoot(self, now_mouse_down: bool) -> Optional[Tuple[Projectile, MuzzleFlash]]:
        if not now_mouse_down:
            return None
        if not self.can_shoot():
            return None
        if self.shot_cooldown > 0:
            return None

        if self.ammo_in_mag <= 0:
            self.audio.play("dry")
            self.shot_cooldown = 0.12
            return None

        w = self.weapon
        self.shot_cooldown = 1.0 / max(0.01, w.fire_rate)
        self.ammo_in_mag -= 1

        ang = angle_to(self.facing)
        spread = math.radians(w.spread_deg)
        ang += random.uniform(-spread, spread)
        d = from_angle(ang)
        spawn = self.pos + d * (self.radius + 10)
        vel = d * w.bullet_speed
        proj = Projectile(owner="player", pos=spawn, vel=vel, damage=w.damage, radius=PROJECTILE_RADIUS, life=1.8)
        flash = MuzzleFlash(pos=self.pos + d * (self.radius + 8), ang=ang, life=0.06)
        self.audio.play("shoot")
        return proj, flash

    def draw(self, surf: pygame.Surface, cam: Vec2):
        # Choose anim
        if self.rolling:
            frame = self.sheet.get_frame("player_roll", self.anim_t)
        else:
            if self.vel.length_squared() > 30 * 30:
                frame = self.sheet.get_frame("player_run", self.anim_t)
            else:
                frame = self.sheet.get_frame("player_idle", self.anim_t)

        # Rotate to face mouse direction.
        rot = -math.degrees(angle_to(self.facing))
        img = pygame.transform.rotate(frame, rot)

        # Squash & stretch on roll (juicy feel).
        if self.rolling:
            t = clamp(self.roll_t / max(1e-6, ROLL_DURATION), 0.0, 1.0)
            squash = lerp(1.15, 0.85, ease_in_out_cubic(t))
            stretch = lerp(0.85, 1.15, ease_in_out_cubic(t))
            w = max(8, int(img.get_width() * squash))
            h = max(8, int(img.get_height() * stretch))
            img = pygame.transform.smoothscale(img, (w, h))

        # Invuln flicker (damage invuln, NOT roll i-frames)
        if self.invuln_timer > 0:
            if int(self.invuln_timer * 20) % 2 == 0:
                img = img.copy()
                img.fill((255, 255, 255, 180), special_flags=pygame.BLEND_RGBA_ADD)

        # I-frames ring indicator (subtle)
        if self.is_in_iframes():
            x, y = int(self.pos.x + cam.x), int(self.pos.y + cam.y)
            pygame.draw.circle(surf, (120, 255, 220, 80), (x, y), self.radius + 18, 2)

        r = img.get_rect(center=(int(self.pos.x + cam.x), int(self.pos.y + cam.y)))
        surf.blit(img, r)


# -----------------------------
# Enemies + Boss
# -----------------------------
class Enemy:
    def __init__(self, pos: Vec2, sheet: SpriteSheet):
        self.pos = Vec2(pos)
        self.vel = Vec2()
        self.radius = ENEMY_RADIUS
        self.hp = ENEMY_HP
        self.sheet = sheet
        self.anim_t = random.random() * 10.0
        self.flash = 0.0

        self.shoot_timer = random.uniform(0.6, 1.3)
        self.windup = 0.0  # telegraph before shooting
        # Anti-clutter: each enemy gets a stable strafe direction so they naturally
        # orbit and spread instead of forming a single blob.
        self.strafe_dir = random.choice([-1.0, 1.0])

    def alive(self) -> bool:
        return self.hp > 0

    def take_damage(self, dmg: int):
        self.hp -= dmg
        self.flash = 0.12

    def update(self, dt: float, player: Player, walls: List[pygame.Rect], others: List["Enemy"]) -> List[Projectile]:
        self.anim_t += dt
        if self.flash > 0:
            self.flash = max(0.0, self.flash - dt)

        if not self.alive():
            self.vel *= pow(0.02, dt)
            return []

        # Simple "bullet kin" style: approach player, but keep some distance.
        to_p = player.pos - self.pos
        dist = to_p.length()
        desired = Vec2()
        dir_to_p = soft_normalize(to_p)

        # Preferred ring distance: enemies try to hang around this band,
        # then strafe around the player to avoid clumping.
        if dist > 220:
            desired = dir_to_p * ENEMY_SPEED
        elif dist < 150:
            desired = -dir_to_p * (ENEMY_SPEED * 0.95)

        if 150 <= dist <= 320:
            tangent = Vec2(-dir_to_p.y, dir_to_p.x) * (ENEMY_SPEED * 0.55 * self.strafe_dir)
            desired += tangent

        # Separation from other enemies
        sep = Vec2()
        for e in others:
            if e is self or not e.alive():
                continue
            d = self.pos - e.pos
            d2 = d.length_squared()
            # Stronger personal space to prevent “enemy balls”.
            if d2 < (self.radius * 3.2) ** 2 and d2 > 1e-6:
                sep += d.normalize() * (1.0 / max(1.0, math.sqrt(d2)))
        desired += sep * 220.0

        self.vel = self.vel.lerp(desired, 1 - pow(0.0008, dt))
        self.pos += self.vel * dt
        self.pos = resolve_circle_walls(self.pos, self.radius, walls)

        # Shooting with wind-up telegraph
        bullets: List[Projectile] = []
        if self.windup > 0:
            self.windup = max(0.0, self.windup - dt)
            if self.windup == 0:
                # Fire!
                aim = soft_normalize(player.pos - self.pos)
                ang = angle_to(aim) + random.uniform(-0.08, 0.08)
                d = from_angle(ang)
                spawn = self.pos + d * (self.radius + 8)
                vel = d * PROJECTILE_SPEED_ENEMY
                bullets.append(Projectile(owner="enemy", pos=spawn, vel=vel, damage=1, radius=PROJECTILE_RADIUS, life=2.4))
                self.shoot_timer = random.uniform(0.8, 1.45)
        else:
            self.shoot_timer -= dt
            if self.shoot_timer <= 0:
                self.windup = 0.22
        return bullets

    def draw(self, surf: pygame.Surface, cam: Vec2):
        frame = self.sheet.get_frame("enemy_walk", self.anim_t)
        img = frame
        if self.flash > 0:
            img = img.copy()
            img.fill((255, 255, 255, 200), special_flags=pygame.BLEND_RGBA_ADD)
        r = img.get_rect(center=(int(self.pos.x + cam.x), int(self.pos.y + cam.y)))
        surf.blit(img, r)

        # Telegraph ring
        if self.windup > 0 and self.alive():
            x, y = int(self.pos.x + cam.x), int(self.pos.y + cam.y)
            t = 1.0 - (self.windup / 0.22)
            pygame.draw.circle(surf, (255, 200, 140, int(80 + 80 * t)), (x, y), int(self.radius + 10 + 8 * t), 2)


class Boss:
    def __init__(self, pos: Vec2, sheet: SpriteSheet):
        self.pos = Vec2(pos)
        self.vel = Vec2()
        self.radius = BOSS_RADIUS
        self.hp = BOSS_HP
        self.hp_max = BOSS_HP
        self.sheet = sheet
        self.anim_t = 0.0
        self.flash = 0.0

        # Pattern system
        self.pattern = "circle"
        self.pattern_timer = 2.0
        self.telegraph = 0.0

        self.spray_shots_left = 0
        self.spray_interval = 0.0
        self.circle_burst_cooldown = 0.0

    def alive(self) -> bool:
        return self.hp > 0

    def take_damage(self, dmg: int):
        self.hp -= dmg
        self.flash = 0.12

    def update(self, dt: float, player: Player, walls: List[pygame.Rect]) -> List[Projectile]:
        self.anim_t += dt
        if self.flash > 0:
            self.flash = max(0.0, self.flash - dt)

        if not self.alive():
            self.vel *= pow(0.02, dt)
            return []

        # Slow drift / repositioning
        to_p = player.pos - self.pos
        desired = soft_normalize(to_p) * 75.0
        self.vel = self.vel.lerp(desired, 1 - pow(0.0012, dt))
        self.pos += self.vel * dt
        self.pos = resolve_circle_walls(self.pos, self.radius, walls)

        bullets: List[Projectile] = []

        # Pattern switching based on timer + HP (more aggressive when low HP).
        self.pattern_timer -= dt
        if self.pattern_timer <= 0:
            self.pattern = "spray" if self.pattern == "circle" else "circle"
            base = 3.0 if self.hp > self.hp_max * 0.5 else 2.1
            self.pattern_timer = base
            self.telegraph = 0.35

        if self.telegraph > 0:
            self.telegraph = max(0.0, self.telegraph - dt)
            return bullets

        if self.pattern == "circle":
            self.circle_burst_cooldown -= dt
            if self.circle_burst_cooldown <= 0:
                # Circle Burst: radial bullets.
                n = 16 if self.hp > self.hp_max * 0.5 else 22
                base_ang = random.random() * math.tau
                for i in range(n):
                    a = base_ang + (i / n) * math.tau
                    d = from_angle(a)
                    spawn = self.pos + d * (self.radius + 10)
                    vel = d * (PROJECTILE_SPEED_ENEMY * 1.05)
                    bullets.append(Projectile(owner="enemy", pos=spawn, vel=vel, damage=1, radius=PROJECTILE_RADIUS, life=3.0))
                self.circle_burst_cooldown = 1.1 if self.hp > self.hp_max * 0.5 else 0.9

        else:
            # Targeted Spray: repeated shots towards player with cone variance.
            if self.spray_shots_left <= 0:
                self.spray_shots_left = 7 if self.hp > self.hp_max * 0.5 else 11
                self.spray_interval = 0.08

            self.spray_interval -= dt
            if self.spray_interval <= 0 and self.spray_shots_left > 0:
                self.spray_interval = 0.08
                self.spray_shots_left -= 1
                aim = soft_normalize(player.pos - self.pos)
                a0 = angle_to(aim)
                cone = math.radians(10 if self.hp > self.hp_max * 0.5 else 14)
                a = a0 + random.uniform(-cone, cone)
                d = from_angle(a)
                spawn = self.pos + d * (self.radius + 8)
                vel = d * (PROJECTILE_SPEED_ENEMY * 1.15)
                bullets.append(Projectile(owner="enemy", pos=spawn, vel=vel, damage=1, radius=PROJECTILE_RADIUS, life=3.0))

        return bullets

    def draw(self, surf: pygame.Surface, cam: Vec2):
        # Reuse enemy frame but scale up and recolor overlay for boss feel.
        base = self.sheet.get_frame("enemy_walk", self.anim_t)
        img = pygame.transform.smoothscale(base, (base.get_width() * 2, base.get_height() * 2))
        img = img.copy()
        img.fill((40, 80, 255, 40), special_flags=pygame.BLEND_RGBA_ADD)
        if self.flash > 0:
            img.fill((255, 255, 255, 220), special_flags=pygame.BLEND_RGBA_ADD)

        r = img.get_rect(center=(int(self.pos.x + cam.x), int(self.pos.y + cam.y)))
        surf.blit(img, r)

        # Boss telegraph indicator
        if self.telegraph > 0 and self.alive():
            x, y = int(self.pos.x + cam.x), int(self.pos.y + cam.y)
            t = 1.0 - (self.telegraph / 0.35)
            col = (180, 220, 255, int(90 + 120 * t)) if self.pattern == "circle" else (255, 180, 210, int(90 + 120 * t))
            pygame.draw.circle(surf, col, (x, y), int(self.radius + 18 + 12 * t), 3)

        # Boss HP bar
        if self.alive():
            bw = 280
            bh = 12
            x0 = WIDTH // 2 - bw // 2
            y0 = 18
            pygame.draw.rect(surf, (0, 0, 0, 150), (x0 - 2, y0 - 2, bw + 4, bh + 4), border_radius=6)
            fill = int(bw * (self.hp / max(1, self.hp_max)))
            pygame.draw.rect(surf, (40, 160, 255), (x0, y0, fill, bh), border_radius=6)
            pygame.draw.rect(surf, (255, 255, 255, 80), (x0, y0, bw, bh), 2, border_radius=6)


# -----------------------------
# Rooms / dungeon progression
# -----------------------------
class Room:
    def __init__(self, idx: int, seed: int, is_boss: bool = False):
        self.idx = idx
        self.seed = seed
        self.is_boss = is_boss
        self.rng = random.Random(seed)

        self.walls: List[pygame.Rect] = []
        self.doors: Dict[str, pygame.Rect] = {}  # "exit" trigger region
        self.door_locked = True
        self.cleared = False

        self.enemies: List[Enemy] = []
        self.boss: Optional[Boss] = None

        self._build_layout()

    def _build_layout(self):
        # Outer bounds walls: use margin rectangles.
        m = WORLD_MARGIN
        # Walls are thick rectangles covering edges; room area is (m..W-m, m..H-m).
        self.walls.append(pygame.Rect(0, 0, WIDTH, m))
        self.walls.append(pygame.Rect(0, HEIGHT - m, WIDTH, m))
        self.walls.append(pygame.Rect(0, 0, m, HEIGHT))
        self.walls.append(pygame.Rect(WIDTH - m, 0, m, HEIGHT))

        # Internal cover (procedural), not in boss room.
        if not self.is_boss:
            n = 3 + self.idx // 2
            for _ in range(n):
                w = self.rng.randint(60, 120)
                h = self.rng.randint(30, 80)
                x = self.rng.randint(m + 80, WIDTH - m - 80 - w)
                y = self.rng.randint(m + 60, HEIGHT - m - 60 - h)
                self.walls.append(pygame.Rect(x, y, w, h))

        # Door to next room: right side trigger zone (inside margin).
        door_w = 22
        door_h = 96
        door_x = WIDTH - m - door_w
        door_y = HEIGHT // 2 - door_h // 2
        self.doors["exit"] = pygame.Rect(door_x, door_y, door_w, door_h)

    def spawn(self, sheet: SpriteSheet):
        self.enemies.clear()
        self.boss = None

        if self.is_boss:
            self.boss = Boss(pos=Vec2(WIDTH * 0.68, HEIGHT * 0.5), sheet=sheet)
            self.door_locked = True
            self.cleared = False
            return

        # Spawn enemies based on index.
        # Reduced spawn scaling to keep fights readable (less clutter).
        count = min(9, 3 + self.idx)
        for _ in range(count):
            for __ in range(30):
                x = self.rng.randint(WORLD_MARGIN + 90, WIDTH - WORLD_MARGIN - 90)
                y = self.rng.randint(WORLD_MARGIN + 70, HEIGHT - WORLD_MARGIN - 70)
                p = Vec2(x, y)
                if p.distance_to(Vec2(WORLD_MARGIN + 80, HEIGHT / 2)) < 120:
                    continue
                if any(circle_vs_rect(p, ENEMY_RADIUS + 6, w)[0] for w in self.walls):
                    continue
                self.enemies.append(Enemy(pos=p, sheet=sheet))
                break
        self.door_locked = True
        self.cleared = False

    def living_enemies(self) -> int:
        if self.is_boss:
            return 1 if (self.boss and self.boss.alive()) else 0
        return sum(1 for e in self.enemies if e.alive())

    def update_clear_state(self):
        if not self.cleared and self.living_enemies() == 0:
            self.cleared = True
            self.door_locked = False

    def draw(self, surf: pygame.Surface, cam: Vec2):
        # Floor gradient background
        surf.fill((12, 14, 20))
        # Subtle grid lines
        for x in range(0, WIDTH, 48):
            pygame.draw.line(surf, (18, 22, 30), (x + cam.x * 0.2, 0), (x + cam.x * 0.2, HEIGHT), 1)
        for y in range(0, HEIGHT, 48):
            pygame.draw.line(surf, (18, 22, 30), (0, y + cam.y * 0.2), (WIDTH, y + cam.y * 0.2), 1)

        # Walls
        for w in self.walls:
            r = w.move(cam.x, cam.y)
            pygame.draw.rect(surf, (24, 30, 45), r, border_radius=8)
            pygame.draw.rect(surf, (0, 0, 0, 120), r, 2, border_radius=8)

        # Exit door
        d = self.doors["exit"].move(cam.x, cam.y)
        if self.door_locked:
            pygame.draw.rect(surf, (140, 60, 70), d, border_radius=10)
            pygame.draw.rect(surf, (255, 120, 130, 120), d, 2, border_radius=10)
        else:
            pygame.draw.rect(surf, (60, 180, 120), d, border_radius=10)
            pygame.draw.rect(surf, (120, 255, 180, 140), d, 2, border_radius=10)
            # Glow
            g = pygame.Surface((d.w + 30, d.h + 30), pygame.SRCALPHA)
            draw_glow_circle(g, (g.get_width() // 2, g.get_height() // 2), min(d.w, d.h) // 3, (120, 255, 180), glow=16)
            surf.blit(g, (d.centerx - g.get_width() // 2, d.centery - g.get_height() // 2), special_flags=pygame.BLEND_RGBA_ADD)


# -----------------------------
# HUD
# -----------------------------
class HUD:
    def __init__(self):
        self.t = 0.0
        self.font_small = pygame.font.Font(None, 20)
        self.font_med = pygame.font.Font(None, 22)
        self.font_room = pygame.font.Font(None, 26)
        self.font_banner = pygame.font.Font(None, 48)
        self._hint_surf = self.font_small.render(
            "WASD move | Mouse aim | LMB shoot | Space dodge | Q switch weapon | F11 fullscreen",
            True,
            (170, 190, 220),
        )

    def update(self, dt: float):
        self.t += dt

    def draw_hearts(self, surf: pygame.Surface, hp: int, hp_max: int):
        # Each heart = 2 hp.
        hearts = math.ceil(hp_max / 2)
        for i in range(hearts):
            x = 16 + i * 34
            y = 18
            fill = clamp((hp - i * 2) / 2.0, 0.0, 1.0)
            bob = int(math.sin(self.t * 2.2 + i * 0.8) * 2)
            self._draw_heart(surf, (x, y + bob), fill)

    def _draw_heart(self, surf: pygame.Surface, pos: Tuple[int, int], fill: float):
        x, y = pos
        # Simple heart from circles + triangle
        base_col = (80, 20, 28)
        full_col = (255, 70, 90)
        outline = (0, 0, 0)

        # Background heart (empty)
        self._heart_shape(surf, x, y, base_col, 220)

        # Filled portion (mask with rect)
        if fill > 0:
            heart = pygame.Surface((26, 22), pygame.SRCALPHA)
            self._heart_shape(heart, 13, 11, full_col, 240)
            clip_h = int(22 * fill)
            clip = pygame.Rect(0, 22 - clip_h, 26, clip_h)
            surf.blit(heart, (x - 13, y - 11), area=clip)

        # Outline
        self._heart_shape(surf, x, y, outline, 180, outline_only=True)

    def _heart_shape(self, surf: pygame.Surface, x: int, y: int, col: Tuple[int, int, int], alpha: int, outline_only: bool = False):
        c = (*col, alpha)
        left = (x - 6, y - 4)
        right = (x + 6, y - 4)
        tri = [(x - 12, y - 2), (x + 12, y - 2), (x, y + 12)]
        if outline_only:
            pygame.draw.circle(surf, c, left, 7, 2)
            pygame.draw.circle(surf, c, right, 7, 2)
            pygame.draw.polygon(surf, c, tri, 2)
        else:
            pygame.draw.circle(surf, c, left, 7)
            pygame.draw.circle(surf, c, right, 7)
            pygame.draw.polygon(surf, c, tri)

    def draw_reload_meter(self, surf: pygame.Surface, meter: float, glow_pulse: float, ammo: int, mag: int, weapon_name: str):
        # Meter is a stylized bar that glows when Perfect Dodge occurs.
        x0 = 18
        y0 = HEIGHT - 34
        w = 230
        h = 14
        pygame.draw.rect(surf, (0, 0, 0, 150), (x0 - 2, y0 - 2, w + 4, h + 4), border_radius=8)

        # Base fill from meter
        fill_w = int(w * clamp(meter, 0.0, 1.0))
        pygame.draw.rect(surf, (70, 140, 255), (x0, y0, fill_w, h), border_radius=8)

        # Glow pulse (adds on top)
        if glow_pulse > 0:
            t = clamp(glow_pulse / 0.7, 0.0, 1.0)
            gsurf = pygame.Surface((w + 30, h + 30), pygame.SRCALPHA)
            col = (130, 255, 210)
            pygame.draw.rect(gsurf, (*col, int(120 * t)), (15, 15, w, h), border_radius=10)
            pygame.draw.rect(gsurf, (*col, int(60 * t)), (10, 10, w + 10, h + 10), 2, border_radius=12)
            surf.blit(gsurf, (x0 - 15, y0 - 15), special_flags=pygame.BLEND_RGBA_ADD)

        pygame.draw.rect(surf, (255, 255, 255, 70), (x0, y0, w, h), 2, border_radius=8)

        # Ammo text
        label = f"{weapon_name}  |  Ammo: {ammo}/{mag}"
        txt = self.font_med.render(label, True, (220, 235, 255))
        surf.blit(txt, (x0 + w + 14, y0 - 2))

        # NOTE: We intentionally do not draw the old "Perfect Dodge reloads/Perfect!" line
        # here because it overlaps with the controls hint on some resolutions.

    def draw_room_label(self, surf: pygame.Surface, label: str):
        txt = self.font_room.render(label, True, (220, 235, 255))
        surf.blit(txt, (WIDTH - txt.get_width() - 18, 16))

    def draw_center_banner(self, surf: pygame.Surface, text: str):
        s = self.font_banner.render(text, True, (245, 250, 255))
        pad = 18
        r = s.get_rect(center=(WIDTH // 2, HEIGHT // 2))
        bg = pygame.Rect(r.left - pad, r.top - pad, r.w + pad * 2, r.h + pad * 2)
        pygame.draw.rect(surf, (0, 0, 0, 160), bg, border_radius=14)
        pygame.draw.rect(surf, (120, 200, 255, 90), bg, 2, border_radius=14)
        surf.blit(s, r)

    def draw_controls_hint(self, surf: pygame.Surface):
        surf.blit(self._hint_surf, (16, HEIGHT - 56))


# -----------------------------
# Game manager
# -----------------------------
class GameManager:
    def __init__(self):
        # Ensure mixer settings are applied before `pygame.init()`.
        pygame.mixer.pre_init(44100, -16, 1, 256)
        pygame.init()
        pygame.display.set_caption("Siphon Dodge Roguelike (Prototype)")
        self.fullscreen = False
        # Use SCALED so it stays crisp if the OS scales.
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.SCALED)
        self.clock = pygame.time.Clock()

        self.audio = AudioBank()
        self.sheet = SpriteSheet(48, 48)
        self.hud = HUD()

        self.running = True

        self.room_idx = 0
        self.room = Room(idx=0, seed=int(time.time()) & 0xFFFF, is_boss=False)
        self.room.spawn(self.sheet)

        self.player = Player(pos=Vec2(WORLD_MARGIN + 80, HEIGHT / 2), sheet=self.sheet, audio=self.audio)

        self.projectiles: List[Projectile] = []
        self.particles: List[Particle] = []
        self.flashes: List[MuzzleFlash] = []

        self.trauma = 0.0  # screen shake amount
        self.freeze_timer = 0.0

        self.fade = 0.0  # room transition fade
        self.fading_out = False
        self.pending_next_room = False

        self.win = False

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        flags = pygame.SCALED
        if self.fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT), flags)

    def reset_run(self):
        self.room_idx = 0
        self.win = False
        self.room = Room(idx=0, seed=int(time.time()) & 0xFFFF, is_boss=False)
        self.room.spawn(self.sheet)
        self.player = Player(pos=Vec2(WORLD_MARGIN + 80, HEIGHT / 2), sheet=self.sheet, audio=self.audio)
        self.projectiles.clear()
        self.particles.clear()
        self.flashes.clear()
        self.trauma = 0.0
        self.freeze_timer = 0.0
        self.fade = 0.0
        self.fading_out = False
        self.pending_next_room = False

    def add_trauma(self, amt: float):
        self.trauma = clamp(self.trauma + amt, 0.0, 1.0)

    def camera_offset(self) -> Vec2:
        # Trauma-based shake: small random offset.
        t = self.trauma * self.trauma
        if t <= 0:
            return Vec2()
        mag = 10 * t
        return Vec2(random.uniform(-mag, mag), random.uniform(-mag, mag))

    def update(self, dt: float):
        # Hitstop / freeze-frame: time scale to 0 for a short burst.
        if self.freeze_timer > 0:
            self.freeze_timer = max(0.0, self.freeze_timer - dt)
            dt_sim = 0.0
        else:
            dt_sim = dt

        # Trauma decay
        self.trauma = max(0.0, self.trauma - dt * 1.8)

        # Transition fade
        if self.fading_out:
            self.fade = min(1.0, self.fade + dt * 2.8)
            if self.fade >= 1.0 and self.pending_next_room:
                self._advance_room()
        else:
            self.fade = max(0.0, self.fade - dt * 2.8)

        # Update HUD timer
        self.hud.update(dt_sim)

        keys = pygame.key.get_pressed()
        mx, my = pygame.mouse.get_pos()
        mouse_world = Vec2(mx, my)  # camera is only shake, so screen==world

        # Input: roll + weapon switch
        if dt_sim > 0:
            # Roll on keydown handled in events; here we just keep player updated.
            self.player.update(dt_sim, self.room.walls, mouse_world, keys)

        # Enemies update + shooting
        if dt_sim > 0 and self.player.hp > 0 and not self.win:
            if self.room.is_boss and self.room.boss:
                bullets = self.room.boss.update(dt_sim, self.player, self.room.walls)
                self.projectiles.extend(bullets)
            else:
                for e in self.room.enemies:
                    bullets = e.update(dt_sim, self.player, self.room.walls, self.room.enemies)
                    self.projectiles.extend(bullets)

        # Projectiles update
        if dt_sim > 0:
            for p in self.projectiles:
                p.update(dt_sim)

        # Muzzle flashes
        if dt_sim > 0:
            for f in self.flashes:
                f.update(dt_sim)

        # Particles
        if dt_sim > 0:
            for part in self.particles:
                part.update(dt_sim)

        self.projectiles = [p for p in self.projectiles if p.active]
        self.flashes = [f for f in self.flashes if f.life > 0]
        self.particles = [pt for pt in self.particles if pt.life > 0]

        # Collision + Perfect Dodge siphon logic
        if dt_sim > 0 and not self.win:
            self._handle_collisions(dt_sim)

        # Room clear checks and door unlock
        self.room.update_clear_state()

        # Check win/loss conditions
        if self.room.is_boss and self.room.boss and (not self.room.boss.alive()) and not self.win:
            self.win = True
            self.room.door_locked = False

        # Door transition trigger (exit door)
        if not self.room.door_locked and not self.fading_out and self.player.hp > 0 and not self.win:
            if circle_vs_rect(self.player.pos, self.player.radius, self.room.doors["exit"])[0]:
                self.fading_out = True
                self.pending_next_room = True

    def _advance_room(self):
        # Called mid-fade when fade reaches 1.0
        self.pending_next_room = False
        self.fading_out = False
        self.fade = 1.0

        self.room_idx += 1
        if self.room_idx >= ROOMS_BEFORE_BOSS:
            is_boss = True
        else:
            is_boss = False
        self.room = Room(idx=self.room_idx, seed=(self.room.seed + 1337 + self.room_idx * 97), is_boss=is_boss)
        self.room.spawn(self.sheet)
        self.projectiles.clear()
        self.particles.clear()
        self.flashes.clear()

        # Spawn player at entrance area
        self.player.pos = Vec2(WORLD_MARGIN + 80, HEIGHT / 2)
        self.player.vel = Vec2()
        self.player.knock = Vec2()

    def _spawn_sparks(self, pos: Vec2, col: Tuple[int, int, int], count: int = 6):
        for _ in range(count):
            a = random.random() * math.tau
            spd = random.uniform(70, 220)
            vel = from_angle(a) * spd
            self.particles.append(Particle(pos=Vec2(pos), vel=vel, life=0.32, max_life=0.32, color=col, radius=random.uniform(2.0, 4.0)))

    def _handle_collisions(self, dt: float):
        """
        ===========================
        Collision Matrix (important)
        ===========================
        Shapes used:
        - Player: circle (center `player.pos`, radius `player.radius`)
        - Enemy: circle
        - Boss: circle
        - Projectile: circle
        - Walls: rects

        Core rules:
        1) Player vs Walls:
           - Always resolves penetration (slide) in `Player.update()` via `resolve_circle_walls()`.

        2) Projectiles vs Walls:
           - Player bullets: destroy on impact + spark particles.
           - Enemy bullets: destroy on impact.

        3) Player bullets vs Enemy/Boss:
           - Deal damage + hit flash.
           - Adds hitstop and small screen shake.

        4) Enemy bullets vs Player:
           - If NOT in roll i-frames: player takes damage.
           - If in roll i-frames: no damage.
           - Additionally, if overlap happens inside the tight PERFECT window, it triggers
             the "Siphon" reload (Perfect Dodge) and refills the player's magazine.

        5) Player roll vs Enemy/Boss hitbox (contact):
           - If overlap happens inside PERFECT window: triggers Siphon reload.
           - Else if player is in i-frames: no damage.
           - Else (not in i-frames): contact damage.

        Key design point:
        - I-frames prevent damage.
        - Perfect Dodge is a stricter timing window inside the i-frames.
        - The ONLY way to refill ammo is by triggering this Perfect Dodge condition.
        """

        # Projectiles vs walls
        for p in self.projectiles:
            if not p.active:
                continue
            # Treat walls as solid; if projectile overlaps any wall rect -> pop.
            for w in self.room.walls:
                if w.collidepoint(int(p.pos.x), int(p.pos.y)):
                    p.active = False
                    if p.owner == "player":
                        self._spawn_sparks(p.pos, (140, 220, 255), 8)
                    break

        # Player bullets vs enemies/boss
        for p in self.projectiles:
            if not p.active or p.owner != "player":
                continue
            # Enemies
            if not self.room.is_boss:
                for e in self.room.enemies:
                    if not e.alive():
                        continue
                    if circle_vs_circle(p.pos, p.radius, e.pos, e.radius):
                        p.active = False
                        e.take_damage(p.damage)
                        self.add_trauma(0.10)
                        self.freeze_timer = max(self.freeze_timer, HITSTOP_ON_HIT)
                        self._spawn_sparks(p.pos, (200, 240, 255), 7)
                        break
            else:
                if self.room.boss and self.room.boss.alive() and circle_vs_circle(p.pos, p.radius, self.room.boss.pos, self.room.boss.radius):
                    p.active = False
                    self.room.boss.take_damage(p.damage)
                    self.add_trauma(0.12)
                    self.freeze_timer = max(self.freeze_timer, HITSTOP_ON_HIT)
                    self._spawn_sparks(p.pos, (210, 240, 255), 10)

        # Enemy bullets vs player
        player_in_iframes = self.player.is_in_iframes()

        for p in self.projectiles:
            if not p.active or p.owner != "enemy":
                continue
            if circle_vs_circle(p.pos, p.radius, self.player.pos, self.player.radius):
                # Overlap occurred.
                if player_in_iframes:
                    # Roll i-frames: bullets cannot damage you.
                    # IMPORTANT: Siphon reload does NOT trigger from bullets anymore.
                    p.active = False
                else:
                    # Take damage normally.
                    self.player.apply_damage(p.damage, knock_dir=(self.player.pos - p.pos))
                    self.add_trauma(0.16)
                    p.active = False
                    self._spawn_sparks(p.pos, (255, 150, 130), 10)

        # Player roll contact vs enemy/boss hitbox (Perfect Dodge through hitbox)
        # This is separate from projectiles: you can roll *through* a living enemy/boss.
        player_in_perfect = self.player.is_in_perfect_window()
        if self.player.hp > 0:
            if self.room.is_boss and self.room.boss and self.room.boss.alive():
                if circle_vs_circle(self.player.pos, self.player.radius, self.room.boss.pos, self.room.boss.radius):
                    if player_in_iframes:
                        if player_in_perfect:
                            if self.player.try_siphon_reload():
                                self.add_trauma(0.20)
                                self.freeze_timer = max(self.freeze_timer, 0.03)
                                self._spawn_sparks(self.player.pos, (120, 255, 200), 18)
                    else:
                        self.player.apply_damage(1, knock_dir=(self.player.pos - self.room.boss.pos))
                        self.add_trauma(0.18)
            elif not self.room.is_boss:
                for e in self.room.enemies:
                    if not e.alive():
                        continue
                    if circle_vs_circle(self.player.pos, self.player.radius, e.pos, e.radius):
                        if player_in_iframes:
                            if player_in_perfect:
                                if self.player.try_siphon_reload():
                                    self.add_trauma(0.18)
                                    self.freeze_timer = max(self.freeze_timer, 0.03)
                                    self._spawn_sparks(self.player.pos, (120, 255, 200), 14)
                        else:
                            self.player.apply_damage(1, knock_dir=(self.player.pos - e.pos))
                            self.add_trauma(0.16)
                        break

        # Cleanup dead enemies and spawn death particles
        if not self.room.is_boss:
            for e in self.room.enemies:
                # If multiple bullets hit on the same frame, HP can drop below 0.
                # We still want exactly one death burst, so we mark processed.
                if e.hp <= 0 and e.hp > -900:
                    e.hp = -999  # mark as processed
                    self._spawn_sparks(e.pos, (255, 120, 130), 18)
                    self.add_trauma(0.10)

    def draw(self):
        cam = self.camera_offset()
        self.room.draw(self.screen, cam)

        # Entities
        for p in self.projectiles:
            p.draw(self.screen, cam)

        if self.room.is_boss and self.room.boss:
            self.room.boss.draw(self.screen, cam)
        else:
            for e in self.room.enemies:
                if e.alive():
                    e.draw(self.screen, cam)

        for f in self.flashes:
            f.draw(self.screen, cam)

        for pt in self.particles:
            pt.draw(self.screen, cam)

        self.player.draw(self.screen, cam)

        # HUD overlay
        self.hud.draw_hearts(self.screen, self.player.hp, self.player.hp_max)

        # Reload meter is driven by the "perfect flash" timer (visual).
        meter = clamp(self.player.perfect_flash / 0.7, 0.0, 1.0)
        self.hud.draw_reload_meter(
            self.screen,
            meter=meter,
            glow_pulse=self.player.perfect_flash,
            ammo=self.player.ammo_in_mag,
            mag=self.player.weapon.mag_size,
            weapon_name=self.player.weapon.name,
        )

        # Win/loss message
        if self.win:
            self.hud.draw_center_banner(self.screen, "YOU WIN!  Press R to restart")
        elif self.player.hp <= 0:
            self.hud.draw_center_banner(self.screen, "YOU DIED  Press R to restart")

        # Controls hint
        self.hud.draw_controls_hint(self.screen)

        # Transition fade
        if self.fade > 0:
            f = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            a = int(255 * clamp(self.fade, 0.0, 1.0))
            f.fill((0, 0, 0, a))
            self.screen.blit(f, (0, 0))

        pygame.display.flip()

    def handle_events(self):
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    self.running = False
                if e.key == pygame.K_F11:
                    self.toggle_fullscreen()
                if e.key == pygame.K_SPACE and self.player.hp > 0 and not self.win:
                    keys = pygame.key.get_pressed()
                    move = Vec2(
                        (1 if keys[pygame.K_d] else 0) - (1 if keys[pygame.K_a] else 0),
                        (1 if keys[pygame.K_s] else 0) - (1 if keys[pygame.K_w] else 0),
                    )
                    self.player.start_roll(move)
                if e.key == pygame.K_q:
                    self.player.switch_weapon()
                if e.key == pygame.K_r and (self.player.hp <= 0 or self.win):
                    self.reset_run()

    def handle_shooting(self):
        if self.player.hp <= 0 or self.win:
            return
        mouse_down = pygame.mouse.get_pressed(num_buttons=3)[0]
        out = self.player.try_shoot(mouse_down)
        if out:
            proj, flash = out
            self.projectiles.append(proj)
            self.flashes.append(flash)
            self.add_trauma(self.player.weapon.kick)
            # tiny muzzle particles
            self._spawn_sparks(flash.pos, (255, 245, 160), 4)

    def run(self):
        while self.running:
            dt = self.clock.tick(FPS) / 1000.0
            dt = clamp(dt, 0.0, 1.0 / 20.0)  # avoid spiral of death
            self.handle_events()
            self.handle_shooting()
            self.update(dt)
            self.draw()

        pygame.quit()


def main():
    # Make sure the process is in a predictable cwd for local runs.
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass

    gm = GameManager()
    gm.run()


if __name__ == "__main__":
    main()

