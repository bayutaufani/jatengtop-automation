-- ============================================================================
-- Migrasi Modul Otomasi Berita Jateng TOP
-- Cara aman: buka admin/auto-news-install.php setelah login admin.
-- File ini disediakan sebagai referensi / untuk dijalankan manual sekali saja
-- lewat phpMyAdmin. JANGAN dijalankan dua kali.
-- ============================================================================

ALTER TABLE `articles`
    ADD COLUMN `caption_instagram` TEXT NULL AFTER `meta_description`,
    ADD COLUMN `caption_tiktok` TEXT NULL AFTER `caption_instagram`,
    ADD COLUMN `sumber_referensi` TEXT NULL AFTER `caption_tiktok`,
    ADD COLUMN `auto_generated` TINYINT(1) NOT NULL DEFAULT 0 AFTER `sumber_referensi`,
    ADD COLUMN `auto_news_type` VARCHAR(20) NULL AFTER `auto_generated`,
    ADD COLUMN `source_hash` VARCHAR(64) NULL AFTER `auto_news_type`,
    ADD INDEX `idx_articles_source_hash` (`source_hash`),
    ADD INDEX `idx_articles_auto_generated` (`auto_generated`);

CREATE TABLE IF NOT EXISTS `auto_news_log` (
    `id` INT(11) NOT NULL AUTO_INCREMENT,
    `run_type` VARCHAR(20) NOT NULL,
    `status` VARCHAR(20) NOT NULL,
    `message` TEXT NULL,
    `items_created` INT(11) NOT NULL DEFAULT 0,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_auto_news_log_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `auto_news_seen` (
    `source_hash` VARCHAR(64) NOT NULL,
    `title` VARCHAR(255) NOT NULL,
    `status` VARCHAR(20) NOT NULL DEFAULT 'seen',
    `seen_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`source_hash`),
    KEY `idx_auto_news_seen_at` (`seen_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
