-- Finans initial schema, generated from backend models.
-- Apply only to a new database; init-db is the preferred path.
SET NAMES utf8mb4;


CREATE TABLE rate_buckets (
	`key` VARCHAR(64) NOT NULL, 
	count INTEGER NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (`key`)
)

;
CREATE INDEX ix_rate_buckets_expires_at ON rate_buckets (expires_at);


CREATE TABLE users (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	name VARCHAR(80) NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	account_number VARCHAR(12) NOT NULL, 
	balance_cents BIGINT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT balance_nonnegative CHECK (balance_cents >= 0), 
	UNIQUE (email), 
	UNIQUE (account_number)
)

;


CREATE TABLE auth_sessions (
	token_hash VARCHAR(64) NOT NULL, 
	user_id INTEGER NOT NULL, 
	csrf VARCHAR(64) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id);
CREATE INDEX ix_auth_sessions_expires_at ON auth_sessions (expires_at);


CREATE TABLE entries (
	id VARCHAR(36) NOT NULL, 
	user_id INTEGER NOT NULL, 
	amount_cents BIGINT NOT NULL, 
	description VARCHAR(120) NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	transfer_id VARCHAR(36), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT entry_nonzero CHECK (amount_cents <> 0), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX ix_entries_user_id ON entries (user_id);
CREATE INDEX ix_entries_created_at ON entries (created_at);
CREATE INDEX ix_entries_transfer_id ON entries (transfer_id);


CREATE TABLE operations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	request_key VARCHAR(64) NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	result_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT operation_unique UNIQUE (user_id, request_key), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;