#!/bin/sh
# Netlify build: vygeneruje web/config.js z proměnných prostředí webu na Netlify.
# Veřejný (publishable/anon) klíč je určený pro prohlížeč – data chrání RLS v Supabase.
set -eu
: "${SUPABASE_URL:?Nastav SUPABASE_URL v Netlify (Site configuration -> Environment variables)}"
: "${SUPABASE_PUBLISHABLE_KEY:?Nastav SUPABASE_PUBLISHABLE_KEY v Netlify}"
cat > web/config.js <<JS
window.APP_CONFIG = {
  supabaseUrl: "${SUPABASE_URL}",
  supabaseKey: "${SUPABASE_PUBLISHABLE_KEY}"
};
JS
echo "web/config.js vygenerován"
