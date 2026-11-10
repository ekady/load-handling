create user if not exists 'repl'@'%' identified by 'repl_password';
grant replication slave on *.* to 'repl'@'%';
flush privileges;

create database if not exists chat_db;
use chat_db;

create table messages_unindexed (
    id bigint auto_increment primary key,
    conversation_id bigint not null,
    user_id int not null,
    message_body text,
    created_at timestamp default current_timestamp
);

create table messages_indexed (
    id bigint auto_increment primary key,
    conversation_id int not null,
    user_id int not null,
    message_body text,
    created_at timestamp default current_timestamp,
    index idx_conversation_time (conversation_id, created_at)
);