#!/bin/sh
# Netlify build: vygeneruje web/config.js z proměnných prostředí webu na Netlify.
# Veřejný (publishable/anon) klíč je určený pro prohlížeč – data chrání RLS v Supabase.
set -eu
: "${SUPABASE_URL:?Nastav SUPABASE_URL v Netlify (Site configuration -> Environment variables)}"
: "${SUPABASE_PUBLISHABLE_KEY:?Nastav SUPABASE_PUBLISHABLE_KEY v Netlify}"
# Datum nasazení pro patičku webu („Poslední aktualizace“)
BUILT="$(TZ=Europe/Prague date '+%-d. %-m. %Y')"
cat > web/config.js <<JS
window.APP_CONFIG = {
  supabaseUrl: "${SUPABASE_URL}",
  supabaseKey: "${SUPABASE_PUBLISHABLE_KEY}",
  built: "${BUILT}"
};
JS
echo "web/config.js vygenerován"
