-- Pre-EDA: cleaned crawl data; no engineered features.
-- URL retained; natural uniqueness applies only without a source ID.
-- MD5 index avoids PostgreSQL B-tree size limits for long comments.
-- Loader checks distinct-text hash collisions before writing.

CREATE TABLE IF NOT EXISTS public.shops (
    shop_id TEXT PRIMARY KEY,
    shop_name TEXT NOT NULL,
    city TEXT NOT NULL,
    region TEXT,
    opening_hour TIME,
    closing_hour TIME,
    price_min INT CHECK (price_min >= 0),
    price_max INT CHECK (price_max >= price_min),
    views INT DEFAULT 0,
    shop_review_count INT DEFAULT 0,
    shop_avg_rating NUMERIC(3,1) CHECK (shop_avg_rating BETWEEN 0 AND 10),
    excellent_count INT DEFAULT 0,
    good_count INT DEFAULT 0,
    average_count INT DEFAULT 0,
    poor_count INT DEFAULT 0,
    location_score NUMERIC(3,1),
    price_score NUMERIC(3,1),
    quality_score NUMERIC(3,1),
    service_score NUMERIC(3,1),
    space_score NUMERIC(3,1),
    last_crawled_at TIMESTAMP NOT NULL DEFAULT now(),
    shop_url TEXT
);

CREATE TABLE IF NOT EXISTS public.users (
    user_pseudo_id TEXT PRIMARY KEY,
    user_name TEXT NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.reviews (
    review_id BIGSERIAL PRIMARY KEY,
    source_review_id BIGINT,
    shop_id TEXT NOT NULL REFERENCES public.shops(shop_id),
    user_pseudo_id TEXT NOT NULL REFERENCES public.users(user_pseudo_id),
    rating NUMERIC(3,1) NOT NULL CHECK (rating BETWEEN 0 AND 10),
    device TEXT,
    comment_datetime TIMESTAMP NOT NULL,
    comment_text TEXT NOT NULL,
    num_photos INT DEFAULT 0 CHECK (num_photos >= 0),
    source_platform TEXT NOT NULL,
    crawled_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_shop ON public.reviews(shop_id);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON public.reviews(user_pseudo_id);
CREATE INDEX IF NOT EXISTS idx_reviews_datetime ON public.reviews(comment_datetime);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_source ON public.reviews(source_review_id) WHERE source_review_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_natural ON public.reviews(shop_id, user_pseudo_id, comment_datetime, md5(comment_text)) WHERE source_review_id IS NULL;

CREATE OR REPLACE VIEW public.foody_reviews_flat AS SELECT r.review_id, r.source_review_id, r.shop_id, r.user_pseudo_id, r.rating, r.device, r.comment_datetime, r.comment_text, r.num_photos, r.source_platform, r.crawled_at, u.user_name, u.first_seen_at, s.shop_name, s.city, s.region, s.opening_hour, s.closing_hour, s.price_min, s.price_max, s.views, s.shop_review_count, s.shop_avg_rating, s.excellent_count, s.good_count, s.average_count, s.poor_count, s.location_score, s.price_score, s.quality_score, s.service_score, s.space_score, s.last_crawled_at, s.shop_url FROM public.reviews r JOIN public.shops s ON r.shop_id = s.shop_id JOIN public.users u ON r.user_pseudo_id = u.user_pseudo_id;
