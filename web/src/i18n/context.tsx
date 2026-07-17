import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from "react";
import type { Locale, Translations } from "./types";
import { en } from "./en";

// `en` is the only eager dictionary (fallback + default). Every other locale is
// loaded on demand so the entry chunk does not ship all ~16 locale files.
const LOCALE_LOADERS = {
  en: () => Promise.resolve(en),
  zh: () => import("./zh").then((m) => m.zh),
  "zh-hant": () => import("./zh-hant").then((m) => m.zhHant),
  ja: () => import("./ja").then((m) => m.ja),
  de: () => import("./de").then((m) => m.de),
  es: () => import("./es").then((m) => m.es),
  fr: () => import("./fr").then((m) => m.fr),
  tr: () => import("./tr").then((m) => m.tr),
  uk: () => import("./uk").then((m) => m.uk),
  af: () => import("./af").then((m) => m.af),
  ko: () => import("./ko").then((m) => m.ko),
  it: () => import("./it").then((m) => m.it),
  ga: () => import("./ga").then((m) => m.ga),
  pt: () => import("./pt").then((m) => m.pt),
  ru: () => import("./ru").then((m) => m.ru),
  hu: () => import("./hu").then((m) => m.hu),
} satisfies Record<Locale, () => Promise<Translations>>;

/** Session-level cache: each locale is fetched at most once. */
const translationsCache = new Map<Locale, Translations>([["en", en]]);
const loadWarnOnce = new Set<Locale>();

async function loadLocale(locale: Locale): Promise<Translations> {
  const cached = translationsCache.get(locale);
  if (cached) return cached;
  try {
    const dict = await LOCALE_LOADERS[locale]();
    translationsCache.set(locale, dict);
    return dict;
  } catch (err) {
    if (!loadWarnOnce.has(locale)) {
      loadWarnOnce.add(locale);
      console.warn(`[i18n] failed to load locale "${locale}", falling back to en`, err);
    }
    return en;
  }
}

// Display metadata for the language picker — endonym (native name) so users
// recognize their language even if they don't speak the current UI language.
// Exposed as a constant so the LanguageSwitcher and any future settings page
// can share the same list.
//
// We intentionally do NOT pair locales with country flags. Languages are not
// countries (English ≠ GB, Portuguese ≠ PT, Spanish ≠ ES, Chinese variants ≠
// any single jurisdiction). Endonyms are unambiguous and avoid the political
// mismapping that flag pairings inevitably create.
export const LOCALE_META: Record<Locale, { name: string }> = {
  en: { name: "English" },
  zh: { name: "简体中文" },
  "zh-hant": { name: "繁體中文" },
  ja: { name: "日本語" },
  de: { name: "Deutsch" },
  es: { name: "Español" },
  fr: { name: "Français" },
  tr: { name: "Türkçe" },
  uk: { name: "Українська" },
  af: { name: "Afrikaans" },
  ko: { name: "한국어" },
  it: { name: "Italiano" },
  ga: { name: "Gaeilge" },
  pt: { name: "Português" },
  ru: { name: "Русский" },
  hu: { name: "Magyar" },
};

const SUPPORTED_LOCALES = Object.keys(LOCALE_LOADERS) as Locale[];
const STORAGE_KEY = "hermes-locale";

function isLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as string[]).includes(value);
}

function getInitialLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && isLocale(stored)) return stored;
  } catch {
    // SSR or privacy mode
  }
  return "en";
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: Translations;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  t: en,
});

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale);
  // Until a non-en dictionary resolves, expose `en` so components never see
  // missing keys / undefined strings during the first paint after switch/boot.
  const [translations, setTranslations] = useState<Translations>(() => {
    const initial = getInitialLocale();
    return translationsCache.get(initial) ?? en;
  });

  useEffect(() => {
    let cancelled = false;
    const cached = translationsCache.get(locale);
    if (cached) {
      setTranslations(cached);
      return;
    }
    setTranslations(en);
    void loadLocale(locale).then((dict) => {
      if (!cancelled) setTranslations(dict);
    });
    return () => {
      cancelled = true;
    };
  }, [locale]);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      // ignore
    }
  }, []);

  const value: I18nContextValue = {
    locale,
    setLocale,
    t: translations,
  };

  return (
    <I18nContext.Provider value={value}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n() {
  return useContext(I18nContext);
}
