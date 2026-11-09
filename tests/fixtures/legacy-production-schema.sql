--
-- PostgreSQL database dump
--


-- Dumped from database version 15.17 (Debian 15.17-1.pgdg13+1)
-- Dumped by pg_dump version 15.17 (Debian 15.17-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attachments (
    id uuid NOT NULL,
    message_id uuid NOT NULL,
    object_id uuid,
    filename character varying(255) NOT NULL,
    file_path character varying(500),
    file_sha256 character varying(64) NOT NULL,
    file_size integer,
    status character varying(50),
    reject_reason character varying(100),
    validation_result jsonb,
    gpt_response jsonb,
    sent_to_email character varying(255),
    sent_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    calculator_number character varying(50),
    sent_filename character varying(255),
    read_receipt_received boolean DEFAULT false,
    read_receipt_at timestamp without time zone,
    original_message_id character varying(255)
);


--
-- Name: email_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_sources (
    id uuid NOT NULL,
    email character varying(255) NOT NULL,
    name character varying(255),
    is_active boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: incoming_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.incoming_messages (
    id uuid NOT NULL,
    provider_message_id character varying(255) NOT NULL,
    source_id uuid NOT NULL,
    from_email character varying(255) NOT NULL,
    subject text,
    parsed_object character varying(255),
    parsed_address character varying(255),
    status character varying(50),
    error_message text,
    received_at timestamp without time zone,
    processed_at timestamp without time zone
);


--
-- Name: objects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.objects (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    name_norm character varying(255) NOT NULL,
    address character varying(255),
    email character varying(255),
    is_active boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    calculator_number character varying(50),
    eldis_id character varying(50),
    object_date character varying(50)
);


--
-- Name: reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reports (
    id uuid NOT NULL,
    attachment_id uuid,
    report_type character varying(50),
    details jsonb,
    created_at timestamp without time zone
);


--
-- Name: settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key character varying(100) NOT NULL,
    value text,
    description character varying(255),
    updated_at timestamp without time zone DEFAULT now()
);


--
-- Name: trusted_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trusted_emails (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(255) NOT NULL,
    description character varying(255),
    is_active boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    username character varying(50) NOT NULL,
    password_hash character varying(255) NOT NULL,
    email character varying(255),
    role character varying(20) DEFAULT 'operator'::character varying,
    is_active boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: attachments attachments_file_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_file_sha256_key UNIQUE (file_sha256);


--
-- Name: attachments attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_pkey PRIMARY KEY (id);


--
-- Name: email_sources email_sources_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_sources
    ADD CONSTRAINT email_sources_email_key UNIQUE (email);


--
-- Name: email_sources email_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_sources
    ADD CONSTRAINT email_sources_pkey PRIMARY KEY (id);


--
-- Name: incoming_messages incoming_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.incoming_messages
    ADD CONSTRAINT incoming_messages_pkey PRIMARY KEY (id);


--
-- Name: incoming_messages incoming_messages_provider_message_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.incoming_messages
    ADD CONSTRAINT incoming_messages_provider_message_id_key UNIQUE (provider_message_id);


--
-- Name: objects objects_calculator_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objects
    ADD CONSTRAINT objects_calculator_number_key UNIQUE (calculator_number);


--
-- Name: objects objects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objects
    ADD CONSTRAINT objects_pkey PRIMARY KEY (id);


--
-- Name: reports reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_pkey PRIMARY KEY (id);


--
-- Name: settings settings_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settings
    ADD CONSTRAINT settings_key_key UNIQUE (key);


--
-- Name: settings settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settings
    ADD CONSTRAINT settings_pkey PRIMARY KEY (id);


--
-- Name: trusted_emails trusted_emails_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trusted_emails
    ADD CONSTRAINT trusted_emails_email_key UNIQUE (email);


--
-- Name: trusted_emails trusted_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trusted_emails
    ADD CONSTRAINT trusted_emails_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: idx_attachments_calculator; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_calculator ON public.attachments USING btree (calculator_number);


--
-- Name: idx_attachments_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_created ON public.attachments USING btree (created_at);


--
-- Name: idx_attachments_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_message ON public.attachments USING btree (message_id);


--
-- Name: idx_attachments_object; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_object ON public.attachments USING btree (object_id);


--
-- Name: idx_attachments_sha256; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_sha256 ON public.attachments USING btree (file_sha256);


--
-- Name: idx_attachments_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attachments_status ON public.attachments USING btree (status);


--
-- Name: idx_email_sources_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_sources_active ON public.email_sources USING btree (is_active);


--
-- Name: idx_messages_provider_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_provider_id ON public.incoming_messages USING btree (provider_message_id);


--
-- Name: idx_messages_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_received ON public.incoming_messages USING btree (received_at);


--
-- Name: idx_messages_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_source ON public.incoming_messages USING btree (source_id);


--
-- Name: idx_messages_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_status ON public.incoming_messages USING btree (status);


--
-- Name: idx_objects_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_objects_active ON public.objects USING btree (is_active);


--
-- Name: idx_objects_calculator; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_objects_calculator ON public.objects USING btree (calculator_number);


--
-- Name: idx_objects_name_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_objects_name_norm ON public.objects USING btree (name_norm);


--
-- Name: idx_reports_attachment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reports_attachment ON public.reports USING btree (attachment_id);


--
-- Name: idx_reports_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reports_created ON public.reports USING btree (created_at);


--
-- Name: idx_reports_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reports_type ON public.reports USING btree (report_type);


--
-- Name: idx_trusted_emails_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trusted_emails_active ON public.trusted_emails USING btree (is_active);


--
-- Name: idx_users_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_active ON public.users USING btree (is_active);


--
-- Name: idx_users_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_username ON public.users USING btree (username);


--
-- Name: attachments attachments_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.incoming_messages(id);


--
-- Name: attachments attachments_object_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_object_id_fkey FOREIGN KEY (object_id) REFERENCES public.objects(id);


--
-- Name: incoming_messages incoming_messages_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.incoming_messages
    ADD CONSTRAINT incoming_messages_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.email_sources(id);


--
-- Name: reports reports_attachment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_attachment_id_fkey FOREIGN KEY (attachment_id) REFERENCES public.attachments(id);


--
-- PostgreSQL database dump complete
--


